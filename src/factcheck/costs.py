"""Custo de cada chamada de API, num lugar só.

Os preços vêm do .env porque não puderam ser lidos na fonte oficial a partir
da nuvem e mudam sem aviso; o valor real é o do painel de cada provedor.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from factcheck.capture.runner import now_local


@dataclass(frozen=True)
class TokenPrice:
    input_per_m: float
    output_per_m: float

    def usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_m + output_tokens * self.output_per_m) / 1_000_000


def record_cost(conn: sqlite3.Connection, provider: str, model: str, purpose: str,
                input_tokens: int, output_tokens: int, usd: float,
                extra: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO api_costs (at, provider, model, purpose, input_tokens, output_tokens, extra, usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now_local().isoformat(), provider, model, purpose, input_tokens, output_tokens,
         json.dumps(extra, ensure_ascii=False) if extra else None, round(usd, 6)),
    )


def total_usd(conn: sqlite3.Connection, provider: str | None = None) -> float:
    if provider:
        row = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM api_costs WHERE provider = ?", (provider,)).fetchone()
    else:
        row = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM api_costs").fetchone()
    return float(row[0])


def summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT provider, purpose, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, ROUND(SUM(usd), 4) AS usd "
        "FROM api_costs GROUP BY provider, purpose ORDER BY provider, purpose").fetchall()
    return [dict(r) for r in rows]
