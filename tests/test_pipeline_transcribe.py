import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from factcheck.config import ConfigError, load_debate_config, load_settings
from factcheck.costs import TokenPrice, total_usd
from factcheck.db import connect
from factcheck.paths import REPO_ROOT
from factcheck.pipeline import transcribe
from factcheck.pipeline.runner import TranscribeWorkers
from factcheck.transcription.base import Segment, Transcript, TranscriptionError

PRICE = TokenPrice(2.5, 10.0)


def add_run(conn, chunks, wall_start=None):
    """chunks: lista de (offset_start, offset_end). Passo de 28 s, como o real."""
    run_id = conn.execute("INSERT INTO capture_runs (source_kind, source_name, started_at, audio_dir) "
                          "VALUES ('file', 'x', '2026-10-23T21:00:00-03:00', '')").lastrowid
    for i, (a, b) in enumerate(chunks, start=1):
        ws = None
        if wall_start:
            from datetime import datetime, timedelta
            ws = (datetime.fromisoformat(wall_start) + timedelta(seconds=a)).isoformat()
        conn.execute("INSERT INTO chunks (run_id, idx, segment, offset_start_s, offset_end_s, wall_start, "
                     "path, mean_dbfs, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, -20, 'x')",
                     (run_id, i, a, b, ws, f"/nao/existe/chunk_{i:05d}.wav"))
    return run_id


class ScriptedTranscriber:
    """Devolve falas definidas em tempo ABSOLUTO, recortadas para cada bloco —
    como o provedor real faria com o áudio que recebeu."""

    def __init__(self, speech, offsets, fail_times=0):
        self.speech = speech          # [(abs_start, abs_end, label, text)]
        self.offsets = offsets        # nome do arquivo → (offset_start, offset_end)
        self.fail_times = fail_times
        self.calls = []

    def transcribe(self, audio_path, references):
        self.calls.append(audio_path.name)
        if self.fail_times:
            self.fail_times -= 1
            raise TranscriptionError("falha simulada")
        a, b = self.offsets[audio_path.name]
        segs = [Segment(lbl, max(s, a) - a, min(e, b) - a, t) for s, e, lbl, t in self.speech if e > a and s < b]
        return Transcript(segs, raw={"segments": len(segs)}, usage={"input_tokens": 600, "output_tokens": 1000})


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "db.sqlite")


def run_all(conn, t, overlap=2.0):
    refs = []
    while (chunk := transcribe.claim_next(conn)) is not None:
        try:
            transcribe.transcribe_chunk(conn, chunk, t, refs, overlap, PRICE, "openai", "m")
        except TranscriptionError as exc:
            transcribe.fail_chunk(conn, chunk, str(exc))


OFFSETS = {"chunk_00001.wav": (0, 30), "chunk_00002.wav": (28, 58), "chunk_00003.wav": (56, 86)}


def test_speech_in_overlap_is_kept_once(conn):
    add_run(conn, [(0, 30), (28, 58), (56, 86)])
    # Fala de 28,2 a 29,6 s está nos blocos 1 e 2; meio = 28,9 < 29 → bloco 1.
    # Fala de 29,3 a 33 s: meio 31,2 → bloco 2.
    speech = [(5, 12, "REF_A", "um"), (28.2, 29.6, "REF_B", "dois"), (29.3, 33, "REF_B", "três"),
              (57, 60, "REF_A", "quatro")]
    run_all(conn, ScriptedTranscriber(speech, OFFSETS))
    texts = [r["text"] for r in conn.execute("SELECT text FROM segments ORDER BY start_s")]
    assert texts == ["um", "dois", "três", "quatro"]


def test_roles_only_from_voice_references(conn):
    add_run(conn, [(0, 30)])
    speech = [(0, 5, "REF_A", "a"), (5, 10, "REF_B", "b"), (10, 15, "REF_M", "m"),
              (15, 20, "A", "voz desconhecida rotulada A")]
    run_all(conn, ScriptedTranscriber(speech, OFFSETS))
    rows = conn.execute("SELECT role, uncertain_reason FROM segments ORDER BY start_s").fetchall()
    assert [r["role"] for r in rows] == ["A", "B", "M", "?"]
    assert rows[3]["uncertain_reason"] == "voz desconhecida"


def test_short_and_overlapping_speech_is_uncertain(conn):
    add_run(conn, [(0, 30)])
    speech = [(1, 1.7, "REF_B", "This is why."), (3, 9, "REF_A", "falando"), (8, 12, "REF_B", "por cima"),
              (14, 20, "REF_A", "limpo")]
    run_all(conn, ScriptedTranscriber(speech, OFFSETS))
    reasons = {r["text"]: r["uncertain_reason"] for r in conn.execute("SELECT * FROM segments")}
    assert reasons == {"This is why.": "fala curta", "falando": "falas sobrepostas",
                       "por cima": "falas sobrepostas", "limpo": None}


def test_wall_time_for_live_chunks(conn):
    add_run(conn, [(0, 30), (28, 58)], wall_start="2026-10-23T21:30:00-03:00")
    run_all(conn, ScriptedTranscriber([(40, 45, "REF_A", "x")], OFFSETS))
    assert conn.execute("SELECT wall_at FROM segments").fetchone()[0] == "2026-10-23T21:30:40-03:00"


def test_failure_retries_then_gives_up(conn):
    add_run(conn, [(0, 30)])
    t = ScriptedTranscriber([], OFFSETS, fail_times=5)
    run_all(conn, t)
    row = conn.execute("SELECT status, attempts, error FROM chunks").fetchone()
    assert (row["status"], row["attempts"]) == ("transcribe_error", 3)
    assert len(t.calls) == 3


def test_transient_failure_recovers_and_costs_are_recorded(conn):
    add_run(conn, [(0, 30)])
    run_all(conn, ScriptedTranscriber([(0, 5, "REF_A", "x")], OFFSETS, fail_times=1))
    assert conn.execute("SELECT status FROM chunks").fetchone()[0] == "transcribed"
    assert total_usd(conn) == pytest.approx((600 * 2.5 + 1000 * 10) / 1e6)


def test_crash_mid_transcription_is_resumed(conn):
    add_run(conn, [(0, 30)])
    transcribe.claim_next(conn)  # processo caiu depois de pegar o bloco
    assert transcribe.claim_next(conn) is None
    assert transcribe.reset_stale(conn) == 1
    run_all(conn, ScriptedTranscriber([(0, 5, "REF_A", "x")], OFFSETS))
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1


def test_workers_refuse_without_candidate_voices(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = load_settings(env_file=tmp_path / "x.env")
    debate = load_debate_config(REPO_ROOT / "config" / "debate.example.toml")
    with pytest.raises(ConfigError, match="vozes de referência"):
        TranscribeWorkers(settings, replace(debate, voices={}), lambda: None, 1)


def test_parallel_workers_process_each_chunk_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = load_settings(env_file=tmp_path / "x.env")
    voice = tmp_path / "v.wav"
    voice.write_bytes(b"RIFF")
    debate = replace(load_debate_config(REPO_ROOT / "config" / "debate.example.toml"),
                     voices={"A": voice, "B": voice, "M": voice})
    conn = connect(settings.db_path)
    spans = [(i * 28, i * 28 + 30) for i in range(8)]
    add_run(conn, spans)
    offsets = {f"chunk_{i + 1:05d}.wav": s for i, s in enumerate(spans)}
    speech = [(i * 28 + 5, i * 28 + 12, "REF_A", f"fala {i}") for i in range(8)]
    shared = ScriptedTranscriber(speech, offsets)
    workers = TranscribeWorkers(settings, debate, lambda: shared, workers=3)
    stop = threading.Event()
    threads = workers.start(stop)
    deadline = time.monotonic() + 10
    while conn.execute("SELECT COUNT(*) FROM chunks WHERE status = 'transcribed'").fetchone()[0] < 8:
        assert time.monotonic() < deadline, "trabalhadores não terminaram"
        time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join(5)
    assert sorted(shared.calls) == sorted(offsets)
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 8
