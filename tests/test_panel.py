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
