"""Etapa 2.5: cada bloco é DEBATE ou INTERVALO. Só debate segue para checagem.

Por que importa: no intervalo passam comerciais e propaganda eleitoral dos
próprios candidatos, cheios de afirmações. Checar isso como fala do debate é
um erro grave — então, na dúvida, é intervalo.

Sinais, nesta ordem:
1. Botão do operador. Vence tudo.
2. Frase de transição do MEDIADOR ("vamos para o intervalo", "estamos de volta").
3. Classificação do bloco pelo Haiku: debate / comercial / vinheta / indefinido.

Regras do plano, aplicadas aqui:
- "indefinido" é intervalo e gera alerta.
- Sair do intervalo exige o botão OU dois blocos seguidos "debate" com falantes
  conhecidos (as vozes de referência reconheceram alguém).
- Bloco sem fala nenhuma mantém o estado anterior: não há o que checar nele, e
  tratá-lo como intervalo faria uma pausa de silêncio custar mais um bloco de
  checagens na volta.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from factcheck.capture.runner import now_local
from factcheck.db import transaction
from factcheck.text import normalize

log = logging.getLogger("factcheck.interval")

# Comparadas com o texto normalizado (sem acento, minúsculo) da fala do mediador.
BREAK_PHRASES = re.compile(
    r"\b(vamos (para o|pro|a um|fazer um|ao) intervalo|intervalo comercial|"
    r"voltamos (ja|logo|daqui a pouco|em instantes)|fim do (primeiro|segundo|terceiro|quarto|quinto)? ?bloco|"
    r"(encerramos|terminamos) (o|este|esse) bloco|daqui a pouco a gente volta)\b")
RETURN_PHRASES = re.compile(
    r"\b(estamos de volta|voltamos ao debate|de volta ao debate|"
    r"(comeca|vamos comecar|inicia|iniciamos) o (segundo|terceiro|quarto|quinto|ultimo) bloco|"
    r"retomamos o debate)\b")

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["debate", "comercial", "vinheta", "indefinido"]},
        "reason": {"type": "string"},
    },
    "required": ["label", "reason"],
    "additionalProperties": False,
}

CLASSIFY_SYSTEM = """Você classifica trechos de 30 segundos da transmissão de um debate político na TV.

Rótulos:
- debate: candidatos e mediador conversando no estúdio (perguntas, respostas, réplicas, regras do debate).
- comercial: propaganda de produto ou serviço, OU propaganda eleitoral gravada (jingle, narrador, depoimentos, "vote", número de candidato).
- vinheta: abertura, encerramento, chamada da emissora, música de transição.
- indefinido: não dá para saber com segurança.

Pistas de que NÃO é debate: muitas vozes desconhecidas, narração publicitária, marcas, preços, jingles, slogans, mudança brusca de assunto.
Na dúvida entre debate e outra coisa, responda indefinido. Responda em JSON."""


class JsonLLM(Protocol):
    def json_call(self, purpose: str, system: str, user: str, schema: dict[str, Any],
                  max_tokens: int = 4000) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Decision:
    label: str
    source: str
    reason: str
    effective: str          # "debate" | "intervalo"


ROLE_NAMES = {"A": "CANDIDATO_1", "B": "CANDIDATO_2", "M": "MEDIADOR", "?": "VOZ_DESCONHECIDA"}


def describe_chunk(segments: list[sqlite3.Row]) -> tuple[str, dict[str, float]]:
    """Texto para o classificador, sem nomes de ninguém."""
    lines = [f"[{ROLE_NAMES[s['role']]}] {s['text']}" for s in segments]
    total = sum(s["end_s"] - s["start_s"] for s in segments) or 1.0
    unknown = sum(s["end_s"] - s["start_s"] for s in segments if s["role"] == "?")
    stats = {"fala_s": round(total, 1), "fracao_voz_desconhecida": round(unknown / total, 2),
             "vozes_desconhecidas_distintas": len({s["speaker_label"] for s in segments if s["role"] == "?"})}
    return "\n".join(lines), stats


def mediator_signal(segments: list[sqlite3.Row]) -> str | None:
    for s in segments:
        if s["role"] != "M":
            continue
        t = normalize(s["text"])
        if BREAK_PHRASES.search(t):
            return "break"
        if RETURN_PHRASES.search(t):
            return "return"
    return None


def has_known_speakers(segments: list[sqlite3.Row]) -> bool:
    return any(s["role"] in ("A", "B", "M") and not s["uncertain"] for s in segments)


def next_pending(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """O bloco mais antigo pronto para classificar, cujo anterior já foi classificado.
    A ordem importa: a regra da volta olha o bloco anterior."""
    rows = conn.execute(
        "SELECT * FROM chunks WHERE status IN ('transcribed', 'transcribe_error') ORDER BY run_id, idx").fetchall()
    for c in rows:
        prev = conn.execute("SELECT status FROM chunks WHERE run_id = ? AND idx = ?",
                            (c["run_id"], c["idx"] - 1)).fetchone()
        if prev is None or prev["status"] in ("classified", "detected"):
            return c
    return None


def previous_decision(conn: sqlite3.Connection, chunk: sqlite3.Row) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT cc.* FROM chunk_class cc JOIN chunks c ON c.id = cc.chunk_id "
        "WHERE c.run_id = ? AND c.idx = ?", (chunk["run_id"], chunk["idx"] - 1)).fetchone()


def decide(conn: sqlite3.Connection, chunk: sqlite3.Row, llm: JsonLLM) -> Decision:
    state = conn.execute("SELECT * FROM debate_state WHERE id = 1").fetchone()
    segments = conn.execute("SELECT * FROM segments WHERE chunk_id = ? ORDER BY start_s", (chunk["id"],)).fetchall()
    prev = previous_decision(conn, chunk)
    if prev is not None:
        prev_effective, prev_label = prev["effective"], prev["label"]
        prev_known = prev["reason"] is not None and "falantes conhecidos" in (prev["reason"] or "")
    else:
        prev_effective = "debate" if state["status"] == "em_andamento" else "intervalo"
        prev_label, prev_known = None, False

    if state["status"] in ("aguardando", "encerrado"):
        return Decision("indefinido", "estado", f"debate {state['status']}", "intervalo")
    if chunk["status"] == "transcribe_error":
        return Decision("indefinido", "regra", "transcrição falhou: conteúdo desconhecido", "intervalo")
    if state["manual_interval"] == "intervalo":
        return Decision("indefinido", "manual", "operador marcou intervalo", "intervalo")
    if state["manual_interval"] == "debate":
        return Decision("debate", "manual", "operador marcou debate", "debate")
    if not segments:
        return Decision("indefinido", "regra", "sem fala no bloco", prev_effective)

    if (state["resume_after"] and chunk["created_at"] >= state["resume_after"]
            and prev_effective == "intervalo"):
        conn.execute("UPDATE debate_state SET resume_after = NULL WHERE id = 1")
        return Decision("debate", "manual", "operador: voltou ao debate", "debate")

    signal = mediator_signal(segments)
    text, stats = describe_chunk(segments)
    result = llm.json_call("intervalo", CLASSIFY_SYSTEM,
                           f"Estatísticas do trecho: {json.dumps(stats, ensure_ascii=False)}\n\nTranscrição:\n{text}",
                           CLASSIFY_SCHEMA, max_tokens=1000)
    label = result.get("label") if result.get("label") in ("debate", "comercial", "vinheta", "indefinido") else "indefinido"
    reason = str(result.get("reason") or "")[:300]
    known = has_known_speakers(segments)
    if label == "debate" and known:
        reason = f"{reason} [falantes conhecidos]"

    if prev_effective == "debate":
        # A frase de ida ao intervalo vale para os blocos SEGUINTES: o próprio
        # bloco ainda tem fala do debate antes dela.
        if prev_label == "debate" and prev["reason"] and "[ida ao intervalo]" in prev["reason"]:
            return Decision(label, "mediador", "mediador anunciou o intervalo no bloco anterior", "intervalo")
        if signal == "break":
            return Decision(label, "mediador", f"{reason} [ida ao intervalo]", "debate")
        return Decision(label, "modelo", reason, "debate" if label == "debate" else "intervalo")

    # Estávamos no intervalo: sair exige dois "debate" seguidos com falantes conhecidos.
    if label == "debate" and known and prev_label == "debate" and prev_known:
        return Decision(label, "modelo", f"{reason} (2º bloco seguido de debate)", "debate")
    if signal == "return":
        reason = f"{reason} [mediador anunciou a volta; aguardando confirmação]"
    return Decision(label, "modelo", reason, "intervalo")


def classify_one(conn: sqlite3.Connection, chunk: sqlite3.Row, llm: JsonLLM) -> Decision:
    decision = decide(conn, chunk, llm)
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO chunk_class (chunk_id, label, source, reason, effective, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chunk["id"], decision.label, decision.source, decision.reason, decision.effective,
             now_local().isoformat()))
        conn.execute("UPDATE chunks SET status = 'classified' WHERE id = ?", (chunk["id"],))
        state = conn.execute("SELECT status FROM debate_state WHERE id = 1").fetchone()["status"]
        if state in ("em_andamento", "intervalo"):
            new = "em_andamento" if decision.effective == "debate" else "intervalo"
            if new != state:
                conn.execute("UPDATE debate_state SET status = ?, updated_at = ? WHERE id = 1",
                             (new, now_local().isoformat()))
                log.warning("estado do debate: %s → %s (bloco %d: %s)", state, new, chunk["idx"], decision.reason)
    if decision.label == "indefinido" and decision.source == "modelo":
        log.warning("bloco %d INDEFINIDO, tratado como intervalo: %s", chunk["idx"], decision.reason)
    return decision


def classify_failed(conn: sqlite3.Connection, chunk: sqlite3.Row, error: str) -> None:
    """Sem classificação, o bloco é tratado como intervalo — a regra de segurança."""
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO chunk_class (chunk_id, label, source, reason, effective, created_at) "
            "VALUES (?, 'indefinido', 'erro', ?, 'intervalo', ?)",
            (chunk["id"], f"classificação falhou: {error}"[:300], now_local().isoformat()))
        conn.execute("UPDATE chunks SET status = 'classified' WHERE id = ?", (chunk["id"],))
    log.error("bloco %d: classificação falhou (%s); tratado como INTERVALO", chunk["idx"], error)
