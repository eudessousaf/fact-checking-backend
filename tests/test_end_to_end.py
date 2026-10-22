"""Fluxo inteiro com as threads reais e modelos simulados: blocos entram,
timeline.json válido sai — com intervalo respeitado e sem duplicatas."""

import json
import threading
import time
from dataclasses import replace

from factcheck.config import load_debate_config, load_settings
from factcheck.contract import validate_timeline
from factcheck.db import connect
from factcheck.llm.claude import VerifyResult
from factcheck.paths import REPO_ROOT
from factcheck.pipeline.publish import LocalDirUploader, TimelinePublisher
from factcheck.pipeline.runner import AnalysisWorker, PublishLoop, TranscribeWorkers, VerifyWorkers
from factcheck.transcription.base import Segment, Transcript
from tests.test_pipeline_flow import SEARCH, src


class Transcriber:
    # bloco → falas (relativas ao bloco)
    SCRIPT = {
        1: [Segment("REF_M", 0, 3, "Boa noite, começa o debate."),
            Segment("REF_A", 3, 14, "O desemprego caiu para 6,2% em 2025.")],
        2: [Segment("C", 0, 25, "Compre já o carro novo. Vote 99!")],              # comercial
        3: [Segment("REF_B", 2, 12, "Criamos 4 milhões de empregos entre 2019 e 2022.")],
        4: [Segment("REF_B", 2, 12, "Criamos 4 milhões de empregos entre 2019 e 2022.")],
        5: [Segment("REF_A", 2, 12, "A inflação de 2024 foi de 4,8%.")],
    }

    def transcribe(self, audio_path, references):
        idx = int(audio_path.stem.split("_")[1])
        return Transcript(self.SCRIPT[idx], raw={}, usage={"input_tokens": 600, "output_tokens": 1000})


class LLM:
    def json_call(self, purpose, system, user, schema, max_tokens=4000):
        if purpose == "intervalo":
            return {"label": "comercial" if "Compre" in user else "debate", "reason": "simulado"}
        trecho = user.split("TRECHO A ANALISAR:")[1]
        claims = []
        for line in trecho.strip().splitlines():
            tag, text = line[1:].split("] ", 1)
            claims.append({"speaker": tag, "quote": text.rstrip("."), "claim": text, "category": "numero"})
        return {"claims": claims}


class Verifier:
    def verify(self, system, user, schema, max_searches=5, max_tokens=16000):
        raw = {"status": "FALSO", "explanation": "Não confere com os dados.", "what_is_off": "",
               "sources": [src(SEARCH[0]["url"]), src(SEARCH[3]["url"])]}
        return VerifyResult(raw, SEARCH, 1, 0.01, "claude-sonnet-5-5")


def test_whole_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = load_settings(env_file=tmp_path / "x.env")
    voice = tmp_path / "v.wav"
    voice.write_bytes(b"RIFF")
    debate = replace(load_debate_config(REPO_ROOT / "config" / "debate.example.toml"),
                     voices={"A": voice, "B": voice, "M": voice}, veto_minutes=0)
    conn = connect(settings.db_path)
    conn.execute("UPDATE debate_state SET status = 'em_andamento', started_at = '2026-10-23T21:22:00-03:00'")
    run = conn.execute("INSERT INTO capture_runs (source_kind, source_name, started_at, audio_dir) "
                       "VALUES ('pulse','x','x','')").lastrowid
    for i in range(1, 6):
        a = (i - 1) * 28
        conn.execute("INSERT INTO chunks (run_id, idx, segment, offset_start_s, offset_end_s, wall_start, path, "
                     "mean_dbfs, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, -20, '2026-10-23T21:30:00-03:00')",
                     (run, i, a, a + 30, f"2026-10-23T21:{30 + i}:00-03:00", f"/x/chunk_{i:05d}.wav"))

    out = tmp_path / "cdn"
    publisher = TimelinePublisher(debate, "teste", tmp_path / "public" / "timeline.json", [LocalDirUploader(out)])
    stop = threading.Event()
    threads = (TranscribeWorkers(settings, debate, Transcriber, workers=2).start(stop)
               + AnalysisWorker(settings, LLM()).start(stop)
               + VerifyWorkers(settings, debate, Verifier(), workers=2).start(stop)
               + PublishLoop(settings, publisher).start(stop))
    deadline = time.monotonic() + 20
    try:
        while True:
            assert time.monotonic() < deadline, "o fluxo não terminou em 20 s"
            if (out / "timeline.json").exists():
                t = json.loads((out / "timeline.json").read_text())
                if len(t["items"]) == 3:
                    break
            time.sleep(0.2)
    finally:
        stop.set()
        for th in threads:
            th.join(10)

    validate_timeline(t)
    quotes = sorted(i["quote"] for i in t["items"])
    # O comercial (bloco 2) nunca vira checagem; o bloco 3 é o 1º "debate" depois
    # dele e ainda conta como intervalo; o 4 repete a fala do 3 e entra uma vez só.
    assert quotes == ["A inflação de 2024 foi de 4,8%", "Criamos 4 milhões de empregos entre 2019 e 2022",
                      "O desemprego caiu para 6,2% em 2025"]
    assert {i["candidate_id"] for i in t["items"]} == {"A", "B"}
    assert all("Compre" not in i["quote"] for i in t["items"])
