"""Painel do operador. Só em localhost, com senha do .env.

Nesta etapa ele tem só o indicador de "ouvindo"; mapeamento de falantes,
estado de intervalo e fila entram na etapa 5, nesta mesma aplicação.
"""

# SEM `from __future__ import annotations` neste arquivo: com ele as anotações
# viram texto, o FastAPI não acha `security` (que é local de create_app) ao
# resolvê-las, e passa a tratar as credenciais como parâmetro comum — a rota
# respondia 422 em vez de pedir senha.

import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from factcheck.capture.routing import AudioRouting
from factcheck.capture.runner import now_local
from factcheck.capture.sources import AudioSetupError
from factcheck.config import ConfigError, DebateConfig, Settings, load_debate_config
from factcheck.db import connect

STATIC = Path(__file__).parent / "static"
# A captura grava status a cada 1 s. Sem atualização por 5 s, o processo de
# captura caiu ou travou — e isso é diferente de silêncio no áudio.
STALE_AFTER_S = 5.0
# O status da rota custa 4 chamadas ao pactl; o painel consulta a cada 1 s.
ROUTE_CACHE_S = 3.0
# Cabeçalho exigido em toda ação que muda algo. Um site qualquer aberto no
# mesmo navegador consegue mandar um formulário para 127.0.0.1 — e o navegador
# anexaria a senha já digitada —, mas não consegue pôr um cabeçalho próprio
# sem a permissão de CORS, que este painel nunca dá.
ACTION_HEADER = "X-Painel"

log = logging.getLogger("factcheck.panel")


class ListenRequest(BaseModel):
    on: bool


def create_app(settings: Settings, routing: AudioRouting | None = None,
               debate: DebateConfig | None = None) -> FastAPI:
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

    def require_action_header(request: Request) -> None:
        if request.headers.get(ACTION_HEADER) != "1":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "ação recusada: origem não é o painel")

    auth = [Depends(require_auth)]
    action = [Depends(require_auth), Depends(require_action_header)]
    routing = routing or AudioRouting()
    route_cache: dict[str, Any] = {"at": 0.0, "data": None}

    def route_status(force: bool = False) -> dict[str, Any]:
        if not force and route_cache["data"] is not None and time.monotonic() - route_cache["at"] < ROUTE_CACHE_S:
            return route_cache["data"]
        try:
            data = routing.status().as_dict()
        except AudioSetupError as exc:
            data = {"ok": False, "error": str(exc)}
        route_cache.update(at=time.monotonic(), data=data)
        return data

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

    if debate is None:
        try:
            debate = load_debate_config(settings.debate_config_path)
        except ConfigError:
            debate = None  # o painel ainda serve captura e som sem o debate configurado
    from factcheck.panel.routes import build_router

    app.include_router(build_router(settings, debate, auth, action))

    @app.get("/api/audio/route", dependencies=auth)
    def audio_route() -> dict[str, Any]:
        return route_status()

    @app.post("/api/audio/listen", dependencies=action)
    def audio_listen(body: ListenRequest) -> dict[str, Any]:
        try:
            routing.set_listening(body.on)
        except AudioSetupError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        log.info("operador: som do operador %s", "ligado" if body.on else "silenciado")
        return route_status(force=True)

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
