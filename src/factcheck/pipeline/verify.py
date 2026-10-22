"""Etapa 5: afirmação → veredito com fontes (Sonnet + busca na web).

O prompt pede as regras; o CÓDIGO as garante. Se o modelo devolver algo fora
delas, o resultado vira SEM_EVIDENCIA — que nunca é publicado nem entra no
gráfico. Forçar um título sem evidência é o erro mais provável deste sistema,
e "Não é bem assim" não pode virar a gaveta do "não sei".

Simetria: o prompt recebe só a afirmação. Quem disse nunca entra aqui.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from factcheck.capture.runner import now_local
from factcheck.contract import MAX_EXPLANATION_SENTENCES
from factcheck.costs import total_usd
from factcheck.db import transaction
from factcheck.llm.claude import VerifyResult

log = logging.getLogger("factcheck.verify")

PUBLIC_VERDICTS = ("VERDADEIRO", "FALSO", "NAO_E_BEM_ASSIM")
MIN_SOURCES = 2
MAX_SOURCES = 8
MAX_ATTEMPTS = 3

# Domínio decide o tipo da fonte, não a palavra do modelo.
PRIMARY_SUFFIXES = (
    "ibge.gov.br", "ipea.gov.br", "bcb.gov.br", "tesouro.gov.br", "tesourotransparente.gov.br",
    "portaldatransparencia.gov.br", "in.gov.br", "camara.leg.br", "senado.leg.br", "tse.jus.br",
    "gov.br", "leg.br", "jus.br", "mp.br", "tcu.gov.br",
)
AGENCY_PATTERNS = (
    re.compile(r"(^|\.)lupa\.(news|uol\.com\.br)$"),
    re.compile(r"(^|\.)aosfatos\.org$"),
    re.compile(r"(^|\.)boatos\.org$"),
    re.compile(r"(^|\.)e-farsas\.com$"),
    re.compile(r"(^|\.)checamos\.afp\.com$"),
    re.compile(r"(^|\.)projetocomprova\.com\.br$"),
)
AGENCY_PATHS = (
    ("g1.globo.com", "/fato-ou-fake"),
    ("estadao.com.br", "/estadao-verifica"),
    ("noticias.uol.com.br", "/confere"),
    ("piaui.folha.uol.com.br", "/lupa"),
)

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["VERDADEIRO", "FALSO", "NAO_E_BEM_ASSIM", "SEM_EVIDENCIA"]},
        "explanation": {"type": "string"},
        "what_is_off": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "publisher": {"type": "string"},
                    "kind": {"type": "string", "enum": ["primaria", "agencia", "imprensa", "outra"]},
                },
                "required": ["url", "title", "publisher", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["status", "explanation", "what_is_off", "sources"],
    "additionalProperties": False,
}

VERIFY_SYSTEM = """Você é um checador de fatos brasileiro, rigoroso e imparcial.

Recebe UMA afirmação feita num debate presidencial (não importa quem disse). Use a busca na web para encontrar dados, de preferência em fontes primárias (IBGE, Ipea, Banco Central, Tesouro, Portal da Transparência, Diário Oficial, Câmara, Senado, TSE) ou checagens já publicadas por agências reconhecidas (Lupa, Aos Fatos, Estadão Verifica, Fato ou Fake, UOL Confere, AFP Checamos, Comprova).

Classifique:
- VERDADEIRO: confere com os dados.
- FALSO: contradiz os dados.
- NAO_E_BEM_ASSIM: tem base real, mas está exagerada, imprecisa, fora de contexto ou omite algo que muda o sentido. Neste caso, what_is_off DEVE dizer o quê (ex.: "o aumento foi de 40%, não 100%").
- SEM_EVIDENCIA: as fontes encontradas não bastam para nenhum dos três. Use sem hesitar: é muito melhor que forçar um título. NAO_E_BEM_ASSIM NÃO é para quando você não sabe.

Regras:
- Cite pelo menos 2 fontes INDEPENDENTES (sites diferentes), todas copiadas dos resultados da sua busca. Nunca invente URL.
- explanation: no máximo 3 frases, linguagem simples, tom neutro, sem adjetivos sobre quem falou.
- what_is_off: vazio ("") exceto em NAO_E_BEM_ASSIM.
- Considere a data do debate: "hoje" é a data informada.

Ao terminar, chame a ferramenta registrar_veredito exatamente uma vez."""


class Verifier(Protocol):
    def verify(self, system: str, user: str, verdict_schema: dict[str, Any],
               max_searches: int = 5, max_tokens: int = 16000) -> VerifyResult: ...


@dataclass
class Outcome:
    status: str                      # um dos PUBLIC_VERDICTS ou SEM_EVIDENCIA
    explanation: str
    what_is_off: str
    sources: list[dict[str, str]]
    notes: list[str] = field(default_factory=list)


def norm_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower().removeprefix("www."), path, p.query, ""))


def site_of(url: str) -> str:
    """Organização dona do site, para contar fontes INDEPENDENTES:
    agenciadenoticias.ibge.gov.br e ibge.gov.br são a mesma fonte."""
    host = urlsplit(url).netloc.lower().removeprefix("www.").split(":")[0]
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in (
        "gov.br", "com.br", "org.br", "leg.br", "jus.br", "mp.br", "edu.br", "uol.com.br") else 2
    return ".".join(labels[-keep:])


def kind_of(url: str, claimed: str) -> str:
    p = urlsplit(url)
    host = p.netloc.lower().removeprefix("www.")
    if any(host == s or host.endswith("." + s) for s in PRIMARY_SUFFIXES):
        return "primaria"
    if any(rx.search(host) for rx in AGENCY_PATTERNS) or any(
            host.endswith(h) and p.path.startswith(path) for h, path in AGENCY_PATHS):
        return "agencia"
    return "imprensa" if claimed == "imprensa" else "outra"


_SENT = re.compile(r"(?<=[.!?])\s+")


def trim_sentences(text: str, limit: int = MAX_EXPLANATION_SENTENCES) -> str:
    parts = [p for p in _SENT.split(text.strip()) if p]
    return " ".join(parts[:limit])


def validate(raw: dict[str, Any] | None, search_results: list[dict[str, str]]) -> Outcome:
    if not raw:
        return Outcome("SEM_EVIDENCIA", "", "", [], ["o modelo não registrou veredito"])
    notes: list[str] = []
    status = raw.get("status") if raw.get("status") in PUBLIC_VERDICTS + ("SEM_EVIDENCIA",) else "SEM_EVIDENCIA"
    found = {norm_url(r["url"]): r for r in search_results}
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for s in raw.get("sources") or []:
        url = str(s.get("url") or "")
        key = norm_url(url)
        if key not in found:
            notes.append(f"fonte descartada (não veio da busca): {url[:200]}")
            continue
        if key in seen:
            continue
        seen.add(key)
        real = found[key]["url"]
        sources.append({"url": real, "title": (str(s.get("title") or "") or found[key]["title"] or real)[:300],
                        "publisher": str(s.get("publisher") or "")[:120],
                        "kind": kind_of(real, str(s.get("kind") or ""))})
    sources = sources[:MAX_SOURCES]
    explanation = trim_sentences(str(raw.get("explanation") or ""))[:700]
    what_is_off = str(raw.get("what_is_off") or "").strip()[:300]

    if status != "SEM_EVIDENCIA":
        independent = {site_of(s["url"]) for s in sources}
        if len(independent) < MIN_SOURCES:
            notes.append(f"{len(independent)} fonte(s) independente(s) válida(s); mínimo {MIN_SOURCES}")
            status = "SEM_EVIDENCIA"
        elif not explanation:
            notes.append("sem explicação")
            status = "SEM_EVIDENCIA"
        elif status == "NAO_E_BEM_ASSIM" and not what_is_off:
            notes.append("NÃO É BEM ASSIM sem dizer o que está errado")
            status = "SEM_EVIDENCIA"
    if status != "NAO_E_BEM_ASSIM":
        what_is_off = ""
    return Outcome(status, explanation, what_is_off, sources, notes)


def reset_stale(conn: sqlite3.Connection) -> int:
    return conn.execute("UPDATE claims SET status = 'detectada' WHERE status = 'verificando'").rowcount


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    with transaction(conn):
        row = conn.execute("SELECT * FROM claims WHERE status = 'detectada' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE claims SET status = 'verificando', attempts = attempts + 1 WHERE id = ?", (row["id"],))
    return conn.execute("SELECT * FROM claims WHERE id = ?", (row["id"],)).fetchone()


def budget_blocked(conn: sqlite3.Connection, hard_stop_usd: float) -> bool:
    return total_usd(conn, "anthropic") >= hard_stop_usd


def verify_one(conn: sqlite3.Connection, claim: sqlite3.Row, verifier: Verifier,
               debate_date: str, veto_minutes: float) -> Outcome:
    user = f"Data do debate: {debate_date}.\n\nAfirmação a checar:\n{claim['claim']}"
    result = verifier.verify(VERIFY_SYSTEM, user, VERDICT_SCHEMA)
    outcome = validate(result.verdict_input, result.search_results)
    state = conn.execute("SELECT mode, auto_publish FROM debate_state WHERE id = 1").fetchone()
    publish_at = None
    if outcome.status in PUBLIC_VERDICTS and state["mode"] == "ao_vivo" and state["auto_publish"]:
        publish_at = (now_local() + timedelta(minutes=veto_minutes)).isoformat()
    detail = {"veredito_do_modelo": result.verdict_input, "resultados_da_busca": result.search_results,
              "buscas": result.searches, "custo_usd": round(result.usd, 4), "modelo": result.model,
              "observacoes": outcome.notes}
    with transaction(conn):
        conn.execute(
            "UPDATE claims SET status = ?, verdict = ?, explanation = ?, what_is_off = ?, sources_json = ?, "
            "verify_json = ?, publish_at = ?, error = NULL WHERE id = ?",
            ("pronta" if outcome.status in PUBLIC_VERDICTS else "sem_evidencia",
             outcome.status, outcome.explanation, outcome.what_is_off or None,
             json.dumps(outcome.sources, ensure_ascii=False), json.dumps(detail, ensure_ascii=False),
             publish_at, claim["id"]))
    log.info("afirmação %d → %s (%d fonte(s), %d busca(s), US$ %.3f)%s", claim["id"], outcome.status,
             len(outcome.sources), result.searches, result.usd,
             f" — {'; '.join(outcome.notes)}" if outcome.notes else "")
    return outcome


def fail_claim(conn: sqlite3.Connection, claim: sqlite3.Row, error: str) -> None:
    final = claim["attempts"] >= MAX_ATTEMPTS
    conn.execute("UPDATE claims SET status = ?, error = ? WHERE id = ?",
                 ("erro" if final else "detectada", error[:500], claim["id"]))
    (log.error if final else log.warning)("afirmação %d: %s%s", claim["id"], error,
                                          " — desistindo" if final else " — vai tentar de novo")
