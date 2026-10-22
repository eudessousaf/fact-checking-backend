"""Etapa 6/7: fila com veto → publicação → timeline.json validado → CDN.

Estados de uma afirmação depois da verificação:
    pronta ──(contagem regressiva acaba, sem pausa)──► publicada ──► removida
      │                                                   ▲
      ├──(operador: publicar agora)───────────────────────┘
      └──(operador: bloquear)──► bloqueada
    sem_evidencia: nunca publica, nunca entra no gráfico.

No pós-debate, `publish_at` nasce vazio: nada publica sozinho, o operador
aprova (um a um ou em lote).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from factcheck.capture.runner import now_local
from factcheck.config import DebateConfig
from factcheck.contract import ContractError, validate_timeline
from factcheck.db import transaction
from factcheck.stats import compute_candidate_stats

log = logging.getLogger("factcheck.publish")

PUBLIC_STATUS = {"aguardando", "em_andamento", "intervalo", "encerrado"}


class PublishError(RuntimeError):
    pass


def log_action(conn: sqlite3.Connection, action: str, detail: dict[str, Any] | str | None = None) -> None:
    conn.execute("INSERT INTO operator_log (at, action, detail) VALUES (?, ?, ?)",
                 (now_local().isoformat(), action,
                  json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail))
    log.info("operador: %s %s", action, detail or "")


def said_at(conn: sqlite3.Connection, claim: sqlite3.Row) -> str | None:
    """Horário em que a fala aconteceu no debate — nunca o do processamento."""
    if claim["wall_at"]:
        return claim["wall_at"]
    anchor = conn.execute("SELECT file_anchor_at FROM debate_state WHERE id = 1").fetchone()[0]
    if not anchor:
        return None
    return (datetime.fromisoformat(anchor) + timedelta(seconds=claim["start_s"])).isoformat()


def publish_due(conn: sqlite3.Connection) -> int:
    state = conn.execute("SELECT publish_paused FROM debate_state WHERE id = 1").fetchone()
    if state["publish_paused"]:
        return 0
    now = now_local().isoformat()
    due = conn.execute("SELECT * FROM claims WHERE status = 'pronta' AND publish_at IS NOT NULL "
                       "AND publish_at <= ?", (now,)).fetchall()
    n = 0
    for c in due:
        if said_at(conn, c) is None:
            log.error("afirmação %d sem horário (falta o início do debate no painel); não publicada", c["id"])
            continue
        conn.execute("UPDATE claims SET status = 'publicada', published_at = ? WHERE id = ? AND status = 'pronta'",
                     (now, c["id"]))
        n += 1
    if n:
        log.info("%d checagem(ns) publicada(s) ao fim da contagem", n)
    return n


# --- ações do operador ---------------------------------------------------------------

def _get(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise PublishError(f"afirmação {claim_id} não existe")
    return row


def publish_now(conn: sqlite3.Connection, claim_id: int) -> None:
    c = _get(conn, claim_id)
    if c["status"] not in ("pronta", "bloqueada"):
        raise PublishError(f"só publica o que está pronto ou bloqueado (está: {c['status']})")
    if c["verdict"] not in ("VERDADEIRO", "FALSO", "NAO_E_BEM_ASSIM"):
        raise PublishError("sem veredito público")
    if said_at(conn, c) is None:
        raise PublishError("falta informar o início do debate (o horário da fala depende dele)")
    with transaction(conn):
        conn.execute("UPDATE claims SET status = 'publicada', published_at = ? WHERE id = ?",
                     (now_local().isoformat(), claim_id))
        log_action(conn, "publicar_agora", {"claim": claim_id})


def block(conn: sqlite3.Connection, claim_id: int) -> None:
    c = _get(conn, claim_id)
    if c["status"] != "pronta":
        raise PublishError(f"só bloqueia o que está na fila (está: {c['status']})")
    with transaction(conn):
        conn.execute("UPDATE claims SET status = 'bloqueada', publish_at = NULL WHERE id = ?", (claim_id,))
        log_action(conn, "bloquear", {"claim": claim_id})


def unpublish(conn: sqlite3.Connection, claim_id: int, note: str | None) -> None:
    c = _get(conn, claim_id)
    if c["status"] != "publicada":
        raise PublishError(f"só despublica o que está publicado (está: {c['status']})")
    with transaction(conn):
        conn.execute("UPDATE claims SET status = 'removida', removed_at = ?, note = ? WHERE id = ?",
                     (now_local().isoformat(), (note or "")[:300] or None, claim_id))
        log_action(conn, "despublicar", {"claim": claim_id, "nota": note})


def edit(conn: sqlite3.Connection, claim_id: int, verdict: str | None, explanation: str | None,
         what_is_off: str | None) -> None:
    c = _get(conn, claim_id)
    if c["status"] not in ("pronta", "bloqueada", "publicada", "sem_evidencia"):
        raise PublishError(f"não dá para editar no estado {c['status']}")
    new_verdict = verdict or c["verdict"]
    if new_verdict not in ("VERDADEIRO", "FALSO", "NAO_E_BEM_ASSIM", "SEM_EVIDENCIA"):
        raise PublishError("veredito inválido")
    new_off = what_is_off if what_is_off is not None else c["what_is_off"]
    if new_verdict == "NAO_E_BEM_ASSIM" and not (new_off or "").strip():
        raise PublishError("NÃO É BEM ASSIM exige dizer o que está errado ou faltando")
    sources = json.loads(c["sources_json"] or "[]")
    if new_verdict != "SEM_EVIDENCIA" and len({s["url"] for s in sources}) < 2:
        raise PublishError("um título público exige pelo menos 2 fontes")
    status = c["status"]
    if new_verdict == "SEM_EVIDENCIA" and status == "publicada":
        raise PublishError("para tirar do ar, use despublicar")
    if new_verdict == "SEM_EVIDENCIA":
        status = "sem_evidencia"
    elif status == "sem_evidencia":
        status = "bloqueada"  # promovido pelo operador: entra na fila, mas não publica sozinho
    with transaction(conn):
        conn.execute(
            "UPDATE claims SET verdict = ?, explanation = ?, what_is_off = ?, status = ?, updated_at = ? WHERE id = ?",
            (new_verdict, explanation if explanation is not None else c["explanation"],
             (new_off or None) if new_verdict == "NAO_E_BEM_ASSIM" else None, status,
             now_local().isoformat() if c["status"] == "publicada" else c["updated_at"], claim_id))
        log_action(conn, "editar", {"claim": claim_id, "de": c["verdict"], "para": new_verdict})


def approve_all_ready(conn: sqlite3.Connection) -> int:
    """Pós-debate: aprova em lote o que o operador já revisou na fila."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM claims WHERE status = 'pronta'")]
    n = 0
    for i in ids:
        try:
            publish_now(conn, i)
            n += 1
        except PublishError as exc:
            log.warning("afirmação %d não aprovada: %s", i, exc)
    return n


# --- timeline.json ------------------------------------------------------------------------

def _item(conn: sqlite3.Connection, c: sqlite3.Row) -> dict[str, Any] | None:
    when = said_at(conn, c)
    if when is None:
        return None
    if c["status"] == "removida":
        item: dict[str, Any] = {"id": f"chk-{c['id']:04d}", "state": "removido", "said_at": when,
                                "candidate_id": c["candidate_id"], "removed_at": c["removed_at"]}
        if c["note"]:
            item["note"] = c["note"]
        return item
    sources = json.loads(c["sources_json"] or "[]")
    item = {
        "id": f"chk-{c['id']:04d}", "state": "publicado", "verdict": c["verdict"], "said_at": when,
        "candidate_id": c["candidate_id"], "quote": c["quote"], "claim": c["claim"],
        "explanation": c["explanation"],
        "indicators": {"source_count": len(sources),
                       "has_primary_source": any(s["kind"] == "primaria" for s in sources),
                       "has_agency_check": any(s["kind"] == "agencia" for s in sources)},
        "sources": [{k: v for k, v in s.items() if v} for s in sources],
        "published_at": c["published_at"],
    }
    if c["verdict"] == "NAO_E_BEM_ASSIM":
        item["what_is_off"] = c["what_is_off"]
    if c["updated_at"]:
        item["updated_at"] = c["updated_at"]
    return item


def build_timeline(conn: sqlite3.Connection, debate: DebateConfig, environment: str) -> dict[str, Any]:
    state = conn.execute("SELECT * FROM debate_state WHERE id = 1").fetchone()
    rows = conn.execute("SELECT * FROM claims WHERE status IN ('publicada', 'removida')").fetchall()
    items = [i for i in (_item(conn, r) for r in rows) if i is not None]
    items.sort(key=lambda i: i["said_at"], reverse=True)
    event: dict[str, Any] = {"title": debate.title, "broadcaster": debate.broadcaster, "timezone": debate.timezone}
    if debate.scheduled_start:
        event["scheduled_start"] = debate.scheduled_start
    event["started_at"] = state["started_at"]
    return {
        "schema_version": "1.0",
        "generated_at": now_local().isoformat(),
        "sequence": state["timeline_seq"],
        "environment": environment,
        "event": event,
        "mode": state["mode"],
        "status": state["status"] if state["status"] in PUBLIC_STATUS else "aguardando",
        "status_updated_at": state["updated_at"],
        "candidates": [{"id": c.id, "name": c.name, "short_name": c.short_name} for c in debate.candidates],
        "stats": {"min_checks_to_display": debate.min_checks_to_display,
                  "by_candidate": compute_candidate_stats(items, [c.id for c in debate.candidates],
                                                          debate.min_checks_to_display)},
        "items": items,
    }


def content_hash(timeline: dict[str, Any]) -> str:
    stable = {k: v for k, v in timeline.items() if k not in ("generated_at", "sequence")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Uploader(Protocol):
    def put(self, name: str, body: bytes, content_type: str) -> None: ...


class LocalDirUploader:
    """Para teste: escreve numa pasta (ex.: a public/ do frontend com o
    `npm run serve`). Em produção, quem recebe é o Spaces."""

    def __init__(self, directory: Path):
        self.directory = directory

    def put(self, name: str, body: bytes, content_type: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.directory / f".{name}.tmp"
        tmp.write_bytes(body)
        tmp.replace(self.directory / name)


@dataclass
class TimelinePublisher:
    debate: DebateConfig
    environment: str
    local_copy: Path
    uploaders: list[Uploader]
    last_hash: str | None = None

    def sync(self, conn: sqlite3.Connection) -> bool:
        """Remonta o timeline; se mudou, valida e envia. Devolve se enviou."""
        timeline = build_timeline(conn, self.debate, self.environment)
        digest = content_hash(timeline)
        if digest == self.last_hash:
            return False
        with transaction(conn):
            conn.execute("UPDATE debate_state SET timeline_seq = timeline_seq + 1 WHERE id = 1")
            timeline["sequence"] = conn.execute("SELECT timeline_seq FROM debate_state WHERE id = 1").fetchone()[0]
        timeline = self._drop_invalid_items(timeline)
        # Arquivo inválido NUNCA sobe: a exceção para aqui e o anterior continua no ar.
        validate_timeline(timeline)
        body = json.dumps(timeline, ensure_ascii=False, separators=(",", ":")).encode()
        self.local_copy.parent.mkdir(parents=True, exist_ok=True)
        self.local_copy.write_bytes(body)
        for up in self.uploaders:
            up.put("timeline.json", body, "application/json; charset=utf-8")
        self.last_hash = digest
        size_kb = len(gzip.compress(body)) / 1024
        log.info("timeline.json enviado (sequência %d, %d itens, %.1f KB comprimido)",
                 timeline["sequence"], len(timeline["items"]), size_kb)
        if size_kb > 200:
            log.warning("timeline.json passou de 200 KB comprimido: hora de dividir o arquivo")
        return True

    def _drop_invalid_items(self, timeline: dict[str, Any]) -> dict[str, Any]:
        """Um item com defeito não pode travar a publicação de todos os outros."""
        try:
            validate_timeline(timeline)
            return timeline
        except ContractError:
            pass
        ids = [c.id for c in self.debate.candidates]
        good = []
        for item in timeline["items"]:
            probe = {**timeline, "items": [item], "stats": {
                **timeline["stats"], "by_candidate": compute_candidate_stats(
                    [item], ids, self.debate.min_checks_to_display)}}
            try:
                validate_timeline(probe)
                good.append(item)
            except ContractError as exc:
                log.error("item %s fora do contrato, NÃO publicado: %s", item.get("id"), "; ".join(exc.problems))
        timeline["items"] = good
        timeline["stats"]["by_candidate"] = compute_candidate_stats(good, ids, self.debate.min_checks_to_display)
        return timeline
