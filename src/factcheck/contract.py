"""Validação de timeline.json e ads.json antes de qualquer envio.

O JSON Schema garante forma; as conferências daqui garantem o que o schema não
consegue expressar (referência a candidato, stats batendo com os itens, limite
de frases). Um arquivo que falhe aqui NUNCA sobe para a CDN.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from factcheck.paths import SCHEMAS_DIR
from factcheck.stats import VERDICTS, largest_remainder_pct

MAX_EXPLANATION_SENTENCES = 3


class ContractError(ValueError):
    def __init__(self, kind: str, problems: list[str]):
        self.kind = kind
        self.problems = problems
        super().__init__(f"{kind} inválido: " + "; ".join(problems))


@cache
def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    # FORMAT_CHECKER só confere date-time e uri se rfc3339-validator e
    # rfc3987-syntax estiverem instalados — por isso o extra
    # `jsonschema[format-nongpl]` no pyproject. Sem eles a checagem passa calada.
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _schema_problems(name: str, data: Any) -> list[str]:
    errors = sorted(_validator(name).iter_errors(data), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '(raiz)'}: {e.message}" for e in errors]


# Conta frases pelo ponto final seguido de espaço/fim. Números como "3,5" e
# "R$ 1.200" não terminam frase porque não têm espaço depois do ponto.
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def count_sentences(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    n = len(_SENTENCE_END.findall(stripped))
    return n if stripped[-1] in ".!?" else n + 1


def _semantic_timeline_problems(data: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    cand_ids = [c["id"] for c in data["candidates"]]
    if len(set(cand_ids)) != len(cand_ids):
        problems.append("candidates: id repetido")

    seen: set[str] = set()
    for item in data["items"]:
        if item["id"] in seen:
            problems.append(f"items: id repetido {item['id']}")
        seen.add(item["id"])
        cid = item.get("candidate_id")
        if cid is not None and cid not in cand_ids:
            problems.append(f"items/{item['id']}: candidate_id {cid} não está em candidates")
        if item["state"] != "publicado":
            continue
        if count_sentences(item["explanation"]) > MAX_EXPLANATION_SENTENCES:
            problems.append(f"items/{item['id']}: explanation passa de {MAX_EXPLANATION_SENTENCES} frases")
        ind = item["indicators"]
        sources = item["sources"]
        if ind["source_count"] != len(sources):
            problems.append(f"items/{item['id']}: indicators.source_count difere do número de fontes")
        if ind["has_primary_source"] != any(s["kind"] == "primaria" for s in sources):
            problems.append(f"items/{item['id']}: has_primary_source não bate com as fontes")
        if ind["has_agency_check"] != any(s["kind"] == "agencia" for s in sources):
            problems.append(f"items/{item['id']}: has_agency_check não bate com as fontes")
        if len({s["url"] for s in sources}) != len(sources):
            problems.append(f"items/{item['id']}: fonte repetida")

    # Os stats publicados precisam ser exatamente os que os itens produzem:
    # gráfico que não bate com a lista embaixo é o defeito que mais tira
    # credibilidade de uma checagem.
    min_display = data["stats"]["min_checks_to_display"]
    by_cid = {s["candidate_id"]: s for s in data["stats"]["by_candidate"]}
    if sorted(by_cid) != sorted(cand_ids):
        problems.append("stats.by_candidate: precisa de exatamente uma entrada por candidato")
    for cid, s in by_cid.items():
        counts = {
            v: sum(1 for i in data["items"]
                   if i["state"] == "publicado" and i.get("candidate_id") == cid and i["verdict"] == v)
            for v in VERDICTS
        }
        # Quando o arquivo é dividido (older_items_url), os itens antigos não
        # estão aqui; aí os stats valem pelo arquivo completo e só conferimos
        # coerência interna.
        if "older_items_url" not in data and any(s[v]["count"] != counts[v] for v in VERDICTS):
            problems.append(f"stats/{cid}: contagem não bate com os itens publicados")
        given = {v: s[v]["count"] for v in VERDICTS}
        if s["total"] != sum(given.values()):
            problems.append(f"stats/{cid}: total diferente da soma das categorias")
        if any(s[v]["pct"] != p for v, p in largest_remainder_pct(given).items()):
            problems.append(f"stats/{cid}: percentuais não correspondem às contagens")
        if s["display"] != (s["total"] >= min_display):
            problems.append(f"stats/{cid}: display incoerente com min_checks_to_display")
    return problems


def validate_timeline(data: Any) -> None:
    problems = _schema_problems("timeline", data)
    if not problems:
        problems = _semantic_timeline_problems(data)
    if problems:
        raise ContractError("timeline.json", problems)


def validate_ads(data: Any) -> None:
    problems = _schema_problems("ads", data)
    if problems:
        raise ContractError("ads.json", problems)


def validate_file(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if path.name.startswith("ads"):
        validate_ads(data)
    else:
        validate_timeline(data)
