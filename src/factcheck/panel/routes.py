# SEM `from __future__ import annotations`: o FastAPI precisa resolver as
# anotações dos modelos de corpo (ver app.py).
"""Rotas do painel: estado do debate, fila com veto, equilíbrio e custos.

Toda ação que muda algo exige o cabeçalho X-Painel (proteção contra cliques
vindos de outros sites) e vai para o operator_log.
"""

import json
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from factcheck.capture.runner import now_local
from factcheck.config import DebateConfig, Settings
from factcheck.costs import total_usd
from factcheck.db import connect, transaction
from factcheck.pipeline import publish

ROLE_LABEL = {"M": "Mediador", "?": "Voz desconhecida"}
CLAIM_STATES = ("detectada", "verificando", "pronta", "bloqueada", "publicada", "removida", "sem_evidencia", "erro")


class StatusBody(BaseModel):
    status: Literal["aguardando", "em_andamento", "encerrado"]


class IntervalBody(BaseModel):
    value: Literal["intervalo", "debate"]


class ModeBody(BaseModel):
    mode: Literal["ao_vivo", "pos_debate"]


class AnchorBody(BaseModel):
    debate_start: str            # ISO com fuso: quando o debate começou de verdade
    file_offset_s: float = 0.0   # em que segundo do arquivo isso acontece (pós-debate)


class PauseBody(BaseModel):
    paused: bool


class EditBody(BaseModel):
    verdict: str | None = None
    explanation: str | None = None
    what_is_off: str | None = None


class NoteBody(BaseModel):
    note: str | None = None


def build_router(settings: Settings, debate: DebateConfig | None, auth, action) -> APIRouter:
    r = APIRouter()
    names = {c.id: c.short_name for c in debate.candidates} if debate else {"A": "Candidato A", "B": "Candidato B"}

    def db() -> sqlite3.Connection:
        return connect(settings.db_path)

    def fail(exc: Exception) -> HTTPException:
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))

    @r.get("/api/overview", dependencies=auth)
    def overview() -> dict[str, Any]:
        conn = db()
        try:
            state = dict(conn.execute("SELECT * FROM debate_state WHERE id = 1").fetchone())
            chunks = {row[0]: row[1] for row in conn.execute("SELECT status, COUNT(*) FROM chunks GROUP BY status")}
            balance = []
            for cid in ("A", "B"):
                counts = {s: 0 for s in CLAIM_STATES}
                for st, n in conn.execute("SELECT status, COUNT(*) FROM claims WHERE candidate_id = ? GROUP BY status", (cid,)):
                    counts[st] = n
                verdicts = {v: n for v, n in conn.execute(
                    "SELECT verdict, COUNT(*) FROM claims WHERE candidate_id = ? AND status = 'publicada' GROUP BY verdict", (cid,))}
                balance.append({"id": cid, "name": names.get(cid, cid), "detected": sum(counts.values()),
                                "checked": sum(counts[s] for s in ("pronta", "bloqueada", "publicada", "removida", "sem_evidencia")),
                                "published": counts["publicada"], "sem_evidencia": counts["sem_evidencia"],
                                "verdicts": verdicts, "states": counts})
            alerts = []
            spent = total_usd(conn, "anthropic")
            if spent >= settings.budget_hard_stop_usd:
                alerts.append(f"TETO DE US$ {settings.budget_hard_stop_usd:.0f} ATINGIDO: novas checagens paradas.")
            elif spent >= settings.budget_warn_usd:
                alerts.append(f"Gasto com o Claude passou de US$ {settings.budget_warn_usd:.0f} (agora: US$ {spent:.2f}).")
            for row in conn.execute(
                    "SELECT c.idx, cc.reason FROM chunk_class cc JOIN chunks c ON c.id = cc.chunk_id "
                    "WHERE cc.label = 'indefinido' AND cc.source IN ('modelo', 'erro') ORDER BY c.id DESC LIMIT 3"):
                alerts.append(f"Bloco {row['idx']} indefinido, tratado como intervalo: {row['reason']}")
            failed = conn.execute("SELECT COUNT(*) FROM chunks WHERE status = 'transcribe_error'").fetchone()[0]
            if failed:
                alerts.append(f"{failed} bloco(s) não transcrito(s) após 3 tentativas (ver log do pipeline).")
            if state["mode"] == "pos_debate" and not state["file_anchor_at"]:
                alerts.append("Pós-debate: informe o início do debate no arquivo, senão nada pode ser publicado.")
            if debate is not None:
                missing = [k for k in ("A", "B", "M") if k not in debate.voices or not debate.voices[k].exists()]
                if missing:
                    alerts.append(f"Vozes de referência faltando: {', '.join(missing)} (config/debate.toml).")
            total_chunks = sum(chunks.values())
            done = chunks.get("detected", 0)
            return {
                "state": state, "chunks": chunks,
                "progress": {"done": done, "total": total_chunks},
                "balance": balance,
                "costs": {"anthropic": round(spent, 4), "openai": round(total_usd(conn, "openai"), 4),
                          "warn": settings.budget_warn_usd, "hard": settings.budget_hard_stop_usd},
                "alerts": alerts,
                "veto_minutes": debate.veto_minutes if debate else None,
                "now": now_local().isoformat(),
            }
        finally:
            conn.close()

    @r.get("/api/claims", dependencies=auth)
    def claims() -> list[dict[str, Any]]:
        conn = db()
        try:
            rows = conn.execute("SELECT * FROM claims ORDER BY id DESC LIMIT 300").fetchall()
            out = []
            for c in rows:
                item = {k: c[k] for k in ("id", "candidate_id", "quote", "claim", "status", "verdict", "explanation",
                                          "what_is_off", "publish_at", "published_at", "removed_at", "note", "error")}
                item["candidate"] = names.get(c["candidate_id"], c["candidate_id"])
                item["said_at"] = publish.said_at(conn, c)
                item["sources"] = json.loads(c["sources_json"] or "[]")
                detail = json.loads(c["verify_json"] or "{}")
                item["notes"] = detail.get("observacoes") or []
                out.append(item)
            return out
        finally:
            conn.close()

    @r.get("/api/transcript", dependencies=auth)
    def transcript() -> list[dict[str, Any]]:
        conn = db()
        try:
            rows = conn.execute("SELECT * FROM segments ORDER BY id DESC LIMIT 25").fetchall()
            return [{"role": s["role"], "who": names.get(s["role"], ROLE_LABEL.get(s["role"], s["role"])),
                     "text": s["text"], "uncertain": s["uncertain_reason"], "at": s["wall_at"],
                     "start_s": round(s["start_s"], 1)} for s in reversed(rows)]
        finally:
            conn.close()

    def _set_state(conn: sqlite3.Connection, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE debate_state SET {sets}, updated_at = ? WHERE id = 1",
                     (*fields.values(), now_local().isoformat()))

    @r.post("/api/debate/status", dependencies=action)
    def set_status(body: StatusBody) -> dict[str, str]:
        conn = db()
        try:
            with transaction(conn):
                st = conn.execute("SELECT started_at, mode FROM debate_state WHERE id = 1").fetchone()
                fields: dict[str, Any] = {"status": body.status, "manual_interval": None}
                if body.status == "em_andamento" and not st["started_at"] and st["mode"] == "ao_vivo":
                    fields["started_at"] = now_local().isoformat()
                _set_state(conn, **fields)
                publish.log_action(conn, "estado_do_debate", {"status": body.status})
            return {"ok": "sim"}
        finally:
            conn.close()

    @r.post("/api/debate/interval", dependencies=action)
    def set_interval(body: IntervalBody) -> dict[str, str]:
        conn = db()
        try:
            with transaction(conn):
                if body.value == "intervalo":
                    _set_state(conn, manual_interval="intervalo", status="intervalo")
                else:
                    # "Voltou ao debate": o próximo bloco gravado depois do clique
                    # já é debate, e daí em diante o automático volta a valer.
                    _set_state(conn, manual_interval=None, resume_after=now_local().isoformat(), status="em_andamento")
                publish.log_action(conn, "intervalo_manual", {"valor": body.value})
            return {"ok": "sim"}
        finally:
            conn.close()

    @r.post("/api/debate/mode", dependencies=action)
    def set_mode(body: ModeBody) -> dict[str, str]:
        conn = db()
        try:
            with transaction(conn):
                _set_state(conn, mode=body.mode, auto_publish=1 if body.mode == "ao_vivo" else 0)
                publish.log_action(conn, "modo", {"modo": body.mode})
            return {"ok": "sim"}
        finally:
            conn.close()

    @r.post("/api/debate/anchor", dependencies=action)
    def set_anchor(body: AnchorBody) -> dict[str, str]:
        from datetime import datetime, timedelta

        try:
            start = datetime.fromisoformat(body.debate_start)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "data/hora inválida") from exc
        if start.tzinfo is None:
            start = start.replace(tzinfo=now_local().tzinfo)
        conn = db()
        try:
            with transaction(conn):
                _set_state(conn, started_at=start.isoformat(),
                           file_anchor_at=(start - timedelta(seconds=body.file_offset_s)).isoformat())
                publish.log_action(conn, "inicio_do_debate", {"inicio": start.isoformat(),
                                                              "segundo_no_arquivo": body.file_offset_s})
            return {"ok": "sim"}
        finally:
            conn.close()

    @r.post("/api/publish/pause", dependencies=action)
    def set_pause(body: PauseBody) -> dict[str, str]:
        conn = db()
        try:
            with transaction(conn):
                _set_state(conn, publish_paused=int(body.paused))
                publish.log_action(conn, "pausa_geral", {"pausado": body.paused})
            return {"ok": "sim"}
        finally:
            conn.close()

    def _claim_action(fn, *args) -> dict[str, str]:
        conn = db()
        try:
            fn(conn, *args)
            return {"ok": "sim"}
        except publish.PublishError as exc:
            raise fail(exc) from exc
        finally:
            conn.close()

    @r.post("/api/claims/{claim_id}/publish", dependencies=action)
    def claim_publish(claim_id: int) -> dict[str, str]:
        return _claim_action(publish.publish_now, claim_id)

    @r.post("/api/claims/{claim_id}/block", dependencies=action)
    def claim_block(claim_id: int) -> dict[str, str]:
        return _claim_action(publish.block, claim_id)

    @r.post("/api/claims/{claim_id}/unpublish", dependencies=action)
    def claim_unpublish(claim_id: int, body: NoteBody) -> dict[str, str]:
        return _claim_action(publish.unpublish, claim_id, body.note)

    @r.post("/api/claims/{claim_id}/edit", dependencies=action)
    def claim_edit(claim_id: int, body: EditBody) -> dict[str, str]:
        return _claim_action(publish.edit, claim_id, body.verdict, body.explanation, body.what_is_off)

    @r.post("/api/claims/approve-all", dependencies=action)
    def approve_all() -> dict[str, int]:
        conn = db()
        try:
            n = publish.approve_all_ready(conn)
            publish.log_action(conn, "aprovar_lote", {"publicadas": n})
            return {"published": n}
        finally:
            conn.close()

    return r
