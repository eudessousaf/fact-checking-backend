from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from factcheck.config import load_settings
from factcheck.db import connect
from factcheck.panel.app import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = load_settings(env_file=tmp_path / "nao-existe.env")
    return replace(s, panel_password="segredo")


def test_panel_refuses_to_start_without_password(settings):
    with pytest.raises(RuntimeError, match="PANEL_PASSWORD"):
        create_app(replace(settings, panel_password=""))


def test_panel_requires_password(settings):
    client = TestClient(create_app(settings))
    assert client.get("/").status_code == 401
    assert client.get("/api/capture/status", auth=("x", "errada")).status_code == 401
    assert client.get("/", auth=("operador", "segredo")).status_code == 200


def test_status_never_started_then_stale(settings):
    client = TestClient(create_app(settings))
    r = client.get("/api/capture/status", auth=("x", "segredo")).json()
    assert r["state"] == "nunca_iniciada"

    conn = connect(settings.db_path)
    conn.execute("INSERT INTO capture_status (id, state, updated_at, level_dbfs) "
                 "VALUES (1, 'ouvindo', '2020-01-01T00:00:00+00:00', -20)")
    r = client.get("/api/capture/status", auth=("x", "segredo")).json()
    # Processo que parou de dar notícia é "parado", não "ouvindo".
    assert r["stale"] is True


def test_audio_actions_require_panel_header(settings):
    from tests.test_routing import FakePactl
    from factcheck.capture.routing import AudioRouting

    routing = AudioRouting(FakePactl())
    routing.setup()
    client = TestClient(create_app(settings, routing=routing))
    auth = ("x", "segredo")
    # Sem o cabeçalho: é o que um formulário de outro site conseguiria mandar.
    assert client.post("/api/audio/listen", json={"on": False}, auth=auth).status_code == 403
    r = client.post("/api/audio/listen", json={"on": False}, auth=auth, headers={"X-Painel": "1"})
    assert r.status_code == 200 and r.json()["loopback_muted"] is True
    assert client.get("/api/audio/route", auth=auth).json()["ok"] is True


def _seed_claim(settings):
    import json as _j
    from factcheck.capture.runner import now_local

    conn = connect(settings.db_path)
    run = conn.execute("INSERT INTO capture_runs (source_kind, source_name, started_at, audio_dir) "
                       "VALUES ('pulse','x','x','')").lastrowid
    ch = conn.execute("INSERT INTO chunks (run_id, idx, segment, offset_start_s, offset_end_s, path, mean_dbfs, "
                      "created_at) VALUES (?, 1, 1, 0, 30, 'x', -20, 'x')", (run,)).lastrowid
    srcs = [{"url": "https://www.ibge.gov.br/a", "title": "IBGE", "publisher": "", "kind": "primaria"},
            {"url": "https://g1.globo.com/fato-ou-fake/b", "title": "G1", "publisher": "", "kind": "agencia"}]
    cid = conn.execute(
        "INSERT INTO claims (chunk_id, run_id, candidate_id, start_s, wall_at, quote, claim, dedup_key, status, "
        "verdict, explanation, sources_json, publish_at, created_at) VALUES (?, ?, 'A', 3, ?, 'q', 'c', 'k', "
        "'pronta', 'FALSO', 'Não confere.', ?, ?, ?)",
        (ch, run, now_local().isoformat(), _j.dumps(srcs), now_local().isoformat(), now_local().isoformat())).lastrowid
    return cid


def test_panel_queue_actions_and_state(settings):
    from factcheck.config import load_debate_config
    from factcheck.paths import REPO_ROOT

    debate = load_debate_config(REPO_ROOT / "config" / "debate.example.toml")
    client = TestClient(create_app(settings, debate=debate))
    auth = ("x", "segredo")
    h = {"X-Painel": "1"}
    cid = _seed_claim(settings)
    assert client.post(f"/api/claims/{cid}/block", auth=auth).status_code == 403   # sem cabeçalho
    assert client.post(f"/api/claims/{cid}/block", auth=auth, headers=h).status_code == 200
    r = client.post(f"/api/claims/{cid}/unpublish", auth=auth, headers=h, json={"note": None})
    assert r.status_code == 409 and "publicado" in r.json()["detail"]
    assert client.post(f"/api/claims/{cid}/publish", auth=auth, headers=h).status_code == 200
    claims = client.get("/api/claims", auth=auth).json()
    assert claims[0]["status"] == "publicada" and claims[0]["candidate"] == "Lula"
    assert client.post("/api/debate/status", auth=auth, headers=h, json={"status": "em_andamento"}).status_code == 200
    assert client.post("/api/debate/interval", auth=auth, headers=h, json={"value": "intervalo"}).status_code == 200
    ov = client.get("/api/overview", auth=auth).json()
    assert ov["state"]["status"] == "intervalo" and ov["state"]["manual_interval"] == "intervalo"
    assert ov["state"]["started_at"]
    assert ov["balance"][0]["published"] == 1 and ov["balance"][1]["published"] == 0
    client.post("/api/debate/interval", auth=auth, headers=h, json={"value": "debate"})
    ov = client.get("/api/overview", auth=auth).json()
    assert ov["state"]["manual_interval"] is None and ov["state"]["resume_after"]
    assert client.post("/api/debate/anchor", auth=auth, headers=h,
                       json={"debate_start": "2026-10-18T22:00:00-03:00", "file_offset_s": 120}).status_code == 200
    ov = client.get("/api/overview", auth=auth).json()
    assert ov["state"]["file_anchor_at"] == "2026-10-18T21:58:00-03:00"
    log = [r[0] for r in connect(settings.db_path).execute("SELECT action FROM operator_log ORDER BY id")]
    assert log == ["bloquear", "publicar_agora", "estado_do_debate", "intervalo_manual", "intervalo_manual",
                   "inicio_do_debate"]
