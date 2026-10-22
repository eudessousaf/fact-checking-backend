"""Etapa 4: falas dos candidatos → afirmações factuais verificáveis (Haiku).

O que o código garante, independentemente do modelo:
- Só entram falas com papel A ou B, SEM marca de incerteza (voz desconhecida,
  fala curta ou sobreposta — ver transcribe.py). Mediador nunca é checado.
- O trecho citado precisa existir na fala DAQUELE candidato neste bloco
  (comparação sem acento e sem pontuação). Citação inventada, ou de outra
  pessoa, é descartada: atribuir fala errada é o pior erro possível.
- O modelo não recebe nome de ninguém, só CANDIDATO_1 / CANDIDATO_2.
- A mesma afirmação repetida não vira duas checagens (índice único por
  candidato + texto normalizado, e as recentes vão no prompt).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from factcheck.capture.runner import now_local
from factcheck.db import transaction
from factcheck.pipeline.interval import JsonLLM
from factcheck.text import normalize

log = logging.getLogger("factcheck.detect")

CONTEXT_S = 60.0
TURN_GAP_S = 1.5
RECENT_CLAIMS = 40
SPEAKER_TAG = {"A": "CANDIDATO_1", "B": "CANDIDATO_2"}
TAG_ROLE = {v: k for k, v in SPEAKER_TAG.items()}

DETECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "enum": ["CANDIDATO_1", "CANDIDATO_2"]},
                    "quote": {"type": "string"},
                    "claim": {"type": "string"},
                    "category": {"type": "string", "enum": [
                        "numero", "data", "lei", "votacao", "gasto", "indicador", "acao_passada", "outro_fato"]},
                },
                "required": ["speaker", "quote", "claim", "category"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}

DETECT_SYSTEM = """Você seleciona, na fala de candidatos num debate, apenas AFIRMAÇÕES FACTUAIS VERIFICÁVEIS.

Inclua: números, datas, fatos sobre leis, votações, gastos, indicadores (emprego, inflação, PIB…) e ações passadas que dá para conferir em fontes públicas.
Exclua: opiniões, promessas, previsões, perguntas, ataques retóricos e juízos de valor.
Exemplo: "você destruiu o país" NÃO é checável; "o desemprego subiu 3 pontos no seu governo" É.

Para cada afirmação:
- speaker: quem disse (CANDIDATO_1 ou CANDIDATO_2), exatamente como marcado na transcrição.
- quote: o trecho LITERAL, copiado da fala daquele candidato, sem mudar nenhuma palavra.
- claim: a afirmação reescrita de forma neutra e verificável, em uma frase, SEM dizer quem falou.
  Se ela depende de um governo ou período, escreva o período por extenso
  (ex.: "O valor médio do Bolsa Família no governo Lula, em 2010, era de R$ 75 por pessoa").
- category: o tipo de fato.

Use só a seção TRECHO A ANALISAR; o CONTEXTO serve para entender referências.
Não repita afirmações da lista JÁ DETECTADAS. Se não houver nada checável, devolva lista vazia. Responda em JSON."""


@dataclass(frozen=True)
class Turn:
    role: str
    start_s: float
    wall_at: str | None
    text: str
    segments: list[sqlite3.Row]


def merge_turns(segments: list[sqlite3.Row]) -> list[Turn]:
    turns: list[Turn] = []
    for s in segments:
        if turns and turns[-1].role == s["role"] and s["start_s"] - turns[-1].segments[-1]["end_s"] <= TURN_GAP_S:
            last = turns[-1]
            turns[-1] = Turn(last.role, last.start_s, last.wall_at, f"{last.text} {s['text']}", last.segments + [s])
        else:
            turns.append(Turn(s["role"], s["start_s"], s["wall_at"], s["text"], [s]))
    return turns


def locate(quote_norm: str, turns: list[Turn], role: str) -> tuple[float, str | None] | None:
    """Onde a citação está na fala daquele candidato; None se não estiver."""
    for t in turns:
        if t.role != role or quote_norm not in normalize(t.text):
            continue
        head = " ".join(quote_norm.split()[:4])
        for s in t.segments:
            if head and head in normalize(s["text"]):
                return s["start_s"], s["wall_at"]
        return t.start_s, t.wall_at
    return None


def next_pending(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT c.*, cc.effective FROM chunks c JOIN chunk_class cc ON cc.chunk_id = c.id "
        "WHERE c.status = 'classified' ORDER BY c.run_id, c.idx LIMIT 1").fetchone()


def detect_one(conn: sqlite3.Connection, chunk: sqlite3.Row, llm: JsonLLM) -> int:
    if chunk["effective"] != "debate":
        conn.execute("UPDATE chunks SET status = 'detected' WHERE id = ?", (chunk["id"],))
        return 0
    segs = conn.execute(
        "SELECT * FROM segments WHERE chunk_id = ? AND role IN ('A', 'B') AND uncertain = 0 ORDER BY start_s",
        (chunk["id"],)).fetchall()
    turns = merge_turns(segs)
    if not turns:
        conn.execute("UPDATE chunks SET status = 'detected' WHERE id = ?", (chunk["id"],))
        return 0

    first = turns[0].start_s
    ctx = conn.execute(
        "SELECT role, text FROM segments WHERE run_id = ? AND start_s >= ? AND start_s < ? ORDER BY start_s",
        (chunk["run_id"], first - CONTEXT_S, first)).fetchall()
    ctx_tags = {"A": "CANDIDATO_1", "B": "CANDIDATO_2", "M": "MEDIADOR", "?": "OUTRA_VOZ"}
    recent = [r["claim"] for r in conn.execute(
        "SELECT claim FROM claims ORDER BY id DESC LIMIT ?", (RECENT_CLAIMS,))]
    user = (
        "CONTEXTO (não analisar):\n" + ("\n".join(f"[{ctx_tags[r['role']]}] {r['text']}" for r in ctx) or "(nenhum)")
        + "\n\nJÁ DETECTADAS (não repetir):\n" + ("\n".join(f"- {c}" for c in recent) or "(nenhuma)")
        + "\n\nTRECHO A ANALISAR:\n" + "\n".join(f"[{SPEAKER_TAG[t.role]}] {t.text}" for t in turns)
    )
    result = llm.json_call("deteccao", DETECT_SYSTEM, user, DETECT_SCHEMA, max_tokens=4000)

    inserted = 0
    now = now_local().isoformat()
    with transaction(conn):
        for c in result.get("claims") or []:
            role = TAG_ROLE.get(c.get("speaker", ""))
            quote = str(c.get("quote") or "").strip()
            claim = str(c.get("claim") or "").strip()
            if role is None or not quote or not claim:
                continue
            where = locate(normalize(quote), turns, role)
            if where is None:
                log.warning("afirmação descartada: citação não está na fala de %s: %r", SPEAKER_TAG[role], quote[:120])
                continue
            start_s, wall_at = where
            cur = conn.execute(
                "INSERT OR IGNORE INTO claims (chunk_id, run_id, candidate_id, start_s, wall_at, quote, claim, "
                "dedup_key, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'detectada', ?)",
                (chunk["id"], chunk["run_id"], role, start_s, wall_at, quote[:600], claim[:300],
                 normalize(claim)[:200], now))
            inserted += cur.rowcount
        conn.execute("UPDATE chunks SET status = 'detected' WHERE id = ?", (chunk["id"],))
    if inserted:
        log.info("bloco %d: %d afirmação(ões) detectada(s)", chunk["idx"], inserted)
    return inserted


def mark_failed(conn: sqlite3.Connection, chunk: sqlite3.Row, error: str) -> None:
    # Falha na detecção não pode travar a fila: o bloco segue sem afirmações, e
    # o erro fica registrado para o operador ver.
    conn.execute("UPDATE chunks SET status = 'detected', error = ? WHERE id = ?", (f"detecção: {error}"[:500], chunk["id"]))
    log.error("bloco %d: detecção falhou (%s); seguindo sem afirmações", chunk["idx"], error)
