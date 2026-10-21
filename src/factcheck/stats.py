"""Contagem de vereditos por candidato — fonte única para a página e o painel."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

VERDICTS = ("VERDADEIRO", "NAO_E_BEM_ASSIM", "FALSO")


def largest_remainder_pct(counts: Mapping[str, int]) -> dict[str, int]:
    """Percentuais inteiros que somam exatamente 100.

    Arredondar cada um sozinho pode dar 99 ou 101 (33+33+33), e a página
    mostraria uma barra que não fecha. Empate no resto é decidido pela ordem
    de VERDICTS, para o resultado ser sempre o mesmo.
    """
    total = sum(counts.values())
    if total == 0:
        return {k: 0 for k in counts}
    raw = {k: counts[k] * 100 / total for k in counts}
    floors = {k: int(v) for k, v in raw.items()}
    missing = 100 - sum(floors.values())
    order = sorted(counts, key=lambda k: (-(raw[k] - floors[k]), list(counts).index(k)))
    for k in order[:missing]:
        floors[k] += 1
    return floors


def compute_candidate_stats(
    items: Iterable[Mapping[str, Any]],
    candidate_ids: Iterable[str],
    min_checks_to_display: int,
) -> list[dict[str, Any]]:
    """Só itens publicados contam; removidos e SEM_EVIDENCIA nunca chegam aqui."""
    published = [i for i in items if i.get("state") == "publicado"]
    result = []
    for cid in candidate_ids:
        counts = {v: 0 for v in VERDICTS}
        for item in published:
            if item.get("candidate_id") == cid:
                counts[item["verdict"]] += 1
        pct = largest_remainder_pct(counts)
        total = sum(counts.values())
        entry: dict[str, Any] = {
            "candidate_id": cid,
            "total": total,
            "display": total >= min_checks_to_display,
        }
        for v in VERDICTS:
            entry[v] = {"count": counts[v], "pct": pct[v]}
        result.append(entry)
    return result
