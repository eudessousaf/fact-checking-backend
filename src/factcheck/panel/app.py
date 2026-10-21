"""Painel do operador. Só em localhost, com senha do .env.

Nesta etapa ele tem só o indicador de "ouvindo"; mapeamento de falantes,
estado de intervalo e fila entram na etapa 5, nesta mesma aplicação.
"""

# SEM `from __future__ import annotations` neste arquivo: com ele as anotações
# viram texto, o FastAPI não acha `security` (que é local de create_app) ao
# resolvê-las, e passa a tratar as credenciais como parâmetro comum — a rota
# respondia 422 em vez de pedir senha.

import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from factcheck.capture.runner import now_local
from factcheck.config import Settings
from factcheck.db import connect

STATIC = Path(__file__).parent / "static"
# A captura grava status a cada 1 s. Sem atualização por 5 s, o processo de
# captura caiu ou travou — e isso é diferente de silêncio no áudio.
STALE_AFTER_S = 5.0


def create_app(settings: Settings) -> FastAPI:
    if not settings.panel_password:
        raise RuntimeError("PANEL_PASSWORD vazio no .env: o painel não sobe sem senha.")

    app = FastAPI(title="Painel do operador", docs_url=None, redoc_url=None, openapi_url=None)
    security = HTTPBasic(realm="painel")

    def require_auth(creds: Annotated[HTTPBasicCredentials, Depends(security)]) -> None:
        # compare_digest: comparação em tempo constante, não revela a senha
        # aos poucos pelo tempo de resposta.
        ok = secrets.compare_digest(creds.password.encode(), settings.panel_password.encode())
        if not ok:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "senha incorreta",
                                headers={"WWW-Authenticate": 'Basic realm="painel"'})

    auth = [Depends(require_auth)]

    @app.get("/", response_class=HTMLResponse, dependencies=auth)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/capture/status", dependencies=auth)
    def capture_status() -> dict[str, Any]:
        conn = connect(settings.db_path)
        try:
            row = conn.execute("SELECT * FROM capture_status WHERE id = 1").fetchone()
        finally:
            conn.close()
        return build_capture_status(row, settings.silence_alert_seconds)

    return app


def build_capture_status(row: Any, silence_alert_s: float) -> dict[str, Any]:
    if row is None:
        return {"state": "nunca_iniciada", "stale": True}
    data = dict(row)
    age = (now_local() - datetime.fromisoformat(data["updated_at"])).total_seconds()
    data["seconds_since_update"] = round(age, 1)
    data["stale"] = data["state"] not in ("parado",) and age > STALE_AFTER_S
    data["silence_alert"] = bool(data["silence_alert"])
    data["silence_alert_after_s"] = silence_alert_s
    if data["last_chunk_at"]:
        data["seconds_since_last_chunk"] = round(
            (now_local() - datetime.fromisoformat(data["last_chunk_at"])).total_seconds(), 1)
    return data
