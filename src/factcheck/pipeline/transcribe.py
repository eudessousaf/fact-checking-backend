"""Estágio de transcrição: bloco capturado → falas com papel (A, B, M ou ?).

Três regras daqui vieram de medição na máquina do operador (22/10/2026,
debate da Band, PROGRESSO.md):

1. Sem vozes de referência os rótulos trocavam quase a cada bloco. Com elas,
   o nome saiu certo nos quatro. Por isso o papel vem SÓ da referência de
   voz; nunca de "o Falante A deste bloco".
2. As referências vão com nomes que o provedor não usa sozinho (REF_A…).
   Sem referência ele rotula "A", "B" — e, se as referências se chamassem
   "A" e "B", uma voz desconhecida rotulada "A" viraria o candidato A.
3. Com referências, um aparte de 0,7 s de outra voz saiu como "Flávio: This
   is why." Fala curta ou sobreposta é marcada `uncertain` e nunca vira
   checagem, mesmo com nome.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from factcheck.capture.runner import now_local
from factcheck.costs import TokenPrice, record_cost
from factcheck.db import transaction
from factcheck.transcription.base import Segment, SpeakerReference, Transcriber, TranscriptionError

log = logging.getLogger("factcheck.transcribe")

REF_PREFIX = "REF_"
ROLES = ("A", "B", "M")
MIN_ATTRIBUTION_S = 2.0
OVERLAP_TOLERANCE_S = 0.2
MAX_ATTEMPTS = 3


def references_for(voices: dict[str, Path]) -> list[SpeakerReference]:
    return [SpeakerReference(f"{REF_PREFIX}{role}", path)
            for role, path in sorted(voices.items()) if role in ROLES]


def role_of(label: str) -> str:
    if label.startswith(REF_PREFIX) and label[len(REF_PREFIX):] in ROLES:
        return label[len(REF_PREFIX):]
    return "?"


@dataclass(frozen=True)
class OwnedWindow:
    """Parte do bloco que é DELE: a sobreposição de 2 s é dividida ao meio
    entre o bloco e o vizinho, para cada fala ficar em um bloco só."""
    start_s: float
    end_s: float

    def owns(self, abs_start: float, abs_end: float) -> bool:
        mid = (abs_start + abs_end) / 2
        return self.start_s <= mid < self.end_s


def owned_window(conn: sqlite3.Connection, chunk: sqlite3.Row, overlap_s: float) -> OwnedWindow:
    half = overlap_s / 2
    has_previous = conn.execute(
        "SELECT 1 FROM chunks WHERE run_id = ? AND segment = ? AND idx < ? LIMIT 1",
        (chunk["run_id"], chunk["segment"], chunk["idx"])).fetchone() is not None
    start = chunk["offset_start_s"] + (half if has_previous else 0.0)
    # O fim sempre perde meia sobreposição: o vizinho seguinte ainda pode não
    # existir, e esperar por ele atrasaria a checagem. No último bloco do
    # debate isso custa 1 s de áudio.
    return OwnedWindow(start, chunk["offset_end_s"] - half)


def mark_uncertain(segments: list[tuple[Segment, str]]) -> list[tuple[Segment, str, str | None]]:
    out = []
    for i, (seg, role) in enumerate(segments):
        reason = None
        if role == "?":
            reason = "voz desconhecida"
        elif seg.end_s - seg.start_s < MIN_ATTRIBUTION_S:
            reason = "fala curta"
        else:
            for j, (other, other_role) in enumerate(segments):
                if j != i and other_role != role and \
                        min(seg.end_s, other.end_s) - max(seg.start_s, other.start_s) > OVERLAP_TOLERANCE_S:
                    reason = "falas sobrepostas"
                    break
        out.append((seg, role, reason))
    return out


def transcribe_chunk(conn: sqlite3.Connection, chunk: sqlite3.Row, transcriber: Transcriber,
                     references: list[SpeakerReference], overlap_s: float, price: TokenPrice,
                     provider: str, model: str) -> int:
    """Transcreve um bloco e grava tudo numa transação. Devolve quantas falas ficaram."""
    result = transcriber.transcribe(Path(chunk["path"]), references)
    window = owned_window(conn, chunk, overlap_s)
    wall_start = datetime.fromisoformat(chunk["wall_start"]) if chunk["wall_start"] else None
    labelled = [(seg, role_of(seg.speaker)) for seg in result.segments]
    rows = []
    for seg, role, reason in mark_uncertain(labelled):
        abs_start = chunk["offset_start_s"] + seg.start_s
        abs_end = chunk["offset_start_s"] + seg.end_s
        if not window.owns(abs_start, abs_end):
            continue
        wall = (wall_start + timedelta(seconds=seg.start_s)).isoformat() if wall_start else None
        rows.append((chunk["id"], chunk["run_id"], abs_start, abs_end, wall, seg.speaker, role,
                     int(reason is not None), reason, seg.text))

    usage = result.usage or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    with transaction(conn):
        conn.execute("INSERT INTO transcripts (chunk_id, provider, raw_json, created_at) VALUES (?, ?, ?, ?)",
                     (chunk["id"], provider, json.dumps(result.raw, ensure_ascii=False), now_local().isoformat()))
        conn.executemany(
            "INSERT INTO segments (chunk_id, run_id, start_s, end_s, wall_at, speaker_label, role, "
            "uncertain, uncertain_reason, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        record_cost(conn, provider, model, "transcricao", in_tok, out_tok, price.usd(in_tok, out_tok),
                    {"chunk_id": chunk["id"]})
        conn.execute("UPDATE chunks SET status = 'transcribed', error = NULL WHERE id = ?", (chunk["id"],))
    return len(rows)


def reset_stale(conn: sqlite3.Connection) -> int:
    """Bloco que ficou 'transcribing' é de um processo que caiu: volta para a fila."""
    return conn.execute("UPDATE chunks SET status = 'captured' WHERE status = 'transcribing'").rowcount


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM chunks WHERE status = 'captured' ORDER BY run_id, idx LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE chunks SET status = 'transcribing', attempts = attempts + 1 WHERE id = ?",
                     (row["id"],))
    return conn.execute("SELECT * FROM chunks WHERE id = ?", (row["id"],)).fetchone()


def fail_chunk(conn: sqlite3.Connection, chunk: sqlite3.Row, error: str) -> None:
    final = chunk["attempts"] >= MAX_ATTEMPTS
    conn.execute("UPDATE chunks SET status = ?, error = ? WHERE id = ?",
                 ("transcribe_error" if final else "captured", error[:500], chunk["id"]))
    (log.error if final else log.warning)("bloco %s (run %s): %s%s", chunk["idx"], chunk["run_id"],
                                          error, " — desistindo" if final else " — vai tentar de novo")
