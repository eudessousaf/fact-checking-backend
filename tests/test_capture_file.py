"""Ponta a ponta da captura com arquivo — o mesmo caminho do pós-debate.

O som do sistema (pulse) não existe na nuvem; este teste cobre tudo o que vem
DEPOIS do ffmpeg, que é idêntico nos dois modos.
"""

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from factcheck.capture.runner import CaptureParams, CaptureRunner, SourceSpec
from factcheck.db import connect
from factcheck.panel.app import build_capture_status

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg ausente")


@pytest.fixture
def tone_then_silence(tmp_path: Path) -> Path:
    # 70 s de tom em estéreo 44,1 kHz (formato diferente do alvo, de propósito)
    # seguidos de 25 s de silêncio.
    out = tmp_path / "entrada.wav"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=70",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=25",
        "-filter_complex", "[0:a]aformat=channel_layouts=stereo[a];[a][1:a]concat=n=2:v=0:a=1",
        str(out)], check=True)
    return out


def test_file_capture_end_to_end(tmp_path: Path, tone_then_silence: Path):
    conn = connect(tmp_path / "db.sqlite")
    params = CaptureParams(chunk_s=30, overlap_s=2, silence_threshold_dbfs=-50,
                           silence_alert_s=20, backup_part_s=40)
    runner = CaptureRunner(conn, tmp_path / "audio", SourceSpec("file", str(tone_then_silence)), params)
    assert runner.run() == "eof"

    rows = conn.execute("SELECT * FROM chunks ORDER BY idx").fetchall()
    assert [(r["offset_start_s"], r["offset_end_s"]) for r in rows] == [
        (0, 30), (28, 58), (56, 86), (84, 95)]
    # Arquivo não tem horário de parede; quem dá o horário é o operador.
    assert all(r["wall_start"] is None for r in rows)
    # O tom do lavfi sai com amplitude 1/8 (~ -24 dBFS); o que importa é a
    # distância para o silêncio.
    assert rows[0]["mean_dbfs"] > -30 and rows[3]["mean_dbfs"] < -90

    for r in rows:
        with wave.open(r["path"]) as w:
            assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
            assert w.getnframes() == round((r["offset_end_s"] - r["offset_start_s"]) * 16000)

    parts = sorted((runner.run_dir / "backup").glob("backup_*.wav"))
    assert len(parts) == 3  # 40 + 40 + 15 s
    total = 0
    for p in parts:
        with wave.open(str(p)) as w:
            total += w.getnframes()
    assert total == 95 * 16000

    status = conn.execute("SELECT * FROM capture_status").fetchone()
    assert status["state"] == "parado"
    assert status["silence_alert"] == 1 and status["silence_seconds"] >= 20
    run = conn.execute("SELECT * FROM capture_runs").fetchone()
    assert run["end_reason"] == "eof" and run["ended_at"]

    view = build_capture_status(status, 20)
    assert view["stale"] is False  # encerrada não é "travada"


def test_resume_from_middle_keeps_file_offsets(tmp_path: Path, tone_then_silence: Path):
    conn = connect(tmp_path / "db.sqlite")
    params = CaptureParams(30, 2, -50, 20)
    spec = SourceSpec("file", str(tone_then_silence), start_s=60)
    CaptureRunner(conn, tmp_path / "audio", spec, params).run()
    rows = conn.execute("SELECT offset_start_s, offset_end_s FROM chunks ORDER BY idx").fetchall()
    assert [tuple(r) for r in rows] == [(60, 90), (88, 95)]


def test_broken_file_fails_loudly(tmp_path: Path):
    bad = tmp_path / "quebrado.wav"
    bad.write_bytes(b"isto nao e audio")
    conn = connect(tmp_path / "db.sqlite")
    runner = CaptureRunner(conn, tmp_path / "audio", SourceSpec("file", str(bad)), CaptureParams(30, 2, -50, 20))
    with pytest.raises(RuntimeError, match="ffmpeg falhou"):
        runner.run()
    assert conn.execute("SELECT end_reason FROM capture_runs").fetchone()[0] == "stopped"


def test_backup_parts_are_read_back_as_one_file(tmp_path: Path, tone_then_silence: Path):
    from factcheck.capture.sources import backup_concat_list

    conn = connect(tmp_path / "db.sqlite")
    first = CaptureRunner(conn, tmp_path / "audio", SourceSpec("file", str(tone_then_silence)),
                          CaptureParams(30, 2, -50, 20, backup_part_s=40))
    first.run()
    listing = backup_concat_list(first.run_dir / "backup", tmp_path / "tmp" / "b.ffconcat")
    second = CaptureRunner(conn, tmp_path / "audio2", SourceSpec("file", str(listing)),
                           CaptureParams(30, 2, -50, 20))
    assert second.run() == "eof"
    assert second.total_samples == 95 * 16000      # as 3 partes, sem buraco nem repetição


def test_resume_offset_from_last_processed_speech(tmp_path: Path, monkeypatch):
    from factcheck.cli import RESUME_MARGIN_S, resume_offset
    from factcheck.config import load_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = load_settings(env_file=tmp_path / "x.env")
    conn = connect(settings.db_path)
    conn.execute("UPDATE debate_state SET started_at = '2026-10-23T21:22:00-03:00'")
    run = conn.execute("INSERT INTO capture_runs (source_kind, source_name, started_at, audio_dir) "
                       "VALUES ('pulse','x','x','')").lastrowid
    ch = conn.execute("INSERT INTO chunks (run_id, idx, segment, offset_start_s, offset_end_s, path, mean_dbfs, "
                      "status, created_at) VALUES (?, 1, 1, 0, 30, 'x', -20, 'detected', 'x')", (run,)).lastrowid
    conn.execute("INSERT INTO segments (chunk_id, run_id, start_s, end_s, wall_at, speaker_label, role, text) "
                 "VALUES (?, ?, 0, 5, '2026-10-23T22:02:00-03:00', 'REF_A', 'A', 'x')", (ch, run))
    # 40 min de debate processados; no arquivo o debate começa no segundo 300.
    assert resume_offset(settings, 300) == 300 + 40 * 60 - RESUME_MARGIN_S
    state = conn.execute("SELECT mode, auto_publish, file_anchor_at FROM debate_state").fetchone()
    assert (state["mode"], state["auto_publish"]) == ("pos_debate", 0)
    assert state["file_anchor_at"] == "2026-10-23T21:17:00-03:00"
