"""Cliente do Claude com registro de custo em toda chamada.

Decisões que não são óbvias:

- Verificação usa `web_search_20250305`, a versão SEM filtragem dinâmica. A
  mais nova filtra os resultados com código antes de devolvê-los, e o plano
  exige provar que cada fonte citada veio da busca — para isso os blocos
  `web_search_tool_result` com as URLs precisam vir na resposta.
- O veredito chega por uma ferramenta `registrar_veredito` com `strict`, e não
  por texto: o formato fica garantido e o código confere o conteúdo depois.
  (`tool_choice` forçado dá 400 no Sonnet 5.5; o prompt manda chamar.)
- `fallbacks: "default"` no Sonnet: se um classificador de segurança recusar,
  o próprio servidor tenta outro modelo. O Haiku não tem esse recurso; uma
  recusa dele vira erro tratado pelo estágio.
- Sem `temperature`: a partir de 21/10/2026 a API recusa com 400.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import anthropic

from factcheck.costs import TokenPrice, record_cost

log = logging.getLogger("factcheck.claude")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
VERDICT_TOOL = "registrar_veredito"
MAX_CONTINUATIONS = 4


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudePrices:
    haiku: TokenPrice
    sonnet: TokenPrice
    web_search_per_1000: float


@dataclass
class VerifyResult:
    verdict_input: dict[str, Any] | None
    search_results: list[dict[str, str]]       # [{url, title}] que a BUSCA devolveu
    searches: int
    usd: float
    model: str
    raw_text: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)


def _text_of(content: list[Any]) -> str:
    return "".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text")


class Claude:
    def __init__(self, api_key: str, conn_factory, prices: ClaudePrices,
                 detect_model: str = "claude-haiku-5-5", verify_model: str = "claude-sonnet-5-5",
                 verify_effort: str = "high", client: Any | None = None):
        if client is None:
            if not api_key:
                raise LLMError("ANTHROPIC_API_KEY vazio no .env")
            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=180.0)
        self.client = client
        self.conn_factory = conn_factory
        self.prices = prices
        self.detect_model = detect_model
        self.verify_model = verify_model
        self.verify_effort = verify_effort

    def _record(self, purpose: str, model: str, in_tok: int, out_tok: int, usd: float,
                extra: dict[str, Any] | None = None) -> None:
        conn: sqlite3.Connection = self.conn_factory()
        try:
            record_cost(conn, "anthropic", model, purpose, in_tok, out_tok, usd, extra)
        finally:
            conn.close()

    # --- Haiku: saída JSON com schema --------------------------------------------

    def json_call(self, purpose: str, system: str, user: str, schema: dict[str, Any],
                  max_tokens: int = 4000) -> dict[str, Any]:
        try:
            resp = self.client.messages.create(
                model=self.detect_model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"{purpose}: API {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"{purpose}: sem conexão com a API: {exc}") from exc
        usage = resp.usage
        self._record(purpose, resp.model, usage.input_tokens, usage.output_tokens,
                     self.prices.haiku.usd(usage.input_tokens, usage.output_tokens))
        if resp.stop_reason == "refusal":
            raise LLMError(f"{purpose}: o modelo recusou ({getattr(resp, 'stop_details', None)})")
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"{purpose}: resposta cortada por max_tokens")
        try:
            return json.loads(_text_of(resp.content))
        except json.JSONDecodeError as exc:
            raise LLMError(f"{purpose}: JSON inválido na resposta") from exc

    # --- Sonnet: busca na web + veredito por ferramenta ------------------------------

    def verify(self, system: str, user: str, verdict_schema: dict[str, Any],
               max_searches: int = 5, max_tokens: int = 16000) -> VerifyResult:
        tools: list[dict[str, Any]] = [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches,
             "user_location": {"type": "approximate", "country": "BR", "timezone": "America/Sao_Paulo"}},
            {"name": VERDICT_TOOL, "strict": True,
             "description": "Registra o resultado final da checagem. Chame exatamente uma vez, no fim.",
             "input_schema": verdict_schema},
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        results: dict[str, dict[str, str]] = {}
        searches = 0
        usd = 0.0
        model = self.verify_model
        text_parts: list[str] = []
        assistant_so_far: list[Any] = []
        for _ in range(MAX_CONTINUATIONS + 1):
            try:
                resp = self.client.beta.messages.create(
                    model=self.verify_model, max_tokens=max_tokens, system=system, messages=messages,
                    tools=tools, output_config={"effort": self.verify_effort},
                    betas=[FALLBACK_BETA], fallbacks="default",
                )
            except anthropic.APIStatusError as exc:
                raise LLMError(f"verificação: API {exc.status_code}: {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError(f"verificação: sem conexão com a API: {exc}") from exc
            model = resp.model
            usage = resp.usage
            n_search = int(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0)
            searches += n_search
            call_usd = (self.prices.sonnet.usd(usage.input_tokens, usage.output_tokens)
                        + n_search * self.prices.web_search_per_1000 / 1000)
            usd += call_usd
            self._record("verificacao", model, usage.input_tokens, usage.output_tokens, call_usd,
                         {"buscas": n_search})
            verdict = None
            for block in resp.content:
                btype = getattr(block, "type", "")
                if btype == "web_search_tool_result" and isinstance(getattr(block, "content", None), list):
                    for r in block.content:
                        url = getattr(r, "url", None)
                        if url:
                            results.setdefault(url, {"url": url, "title": getattr(r, "title", "") or url})
                elif btype == "text":
                    text_parts.append(block.text)
                elif btype == "tool_use" and getattr(block, "name", "") == VERDICT_TOOL:
                    verdict = dict(block.input)
            if resp.stop_reason == "refusal":
                raise LLMError(f"verificação recusada pelo modelo ({getattr(resp, 'stop_details', None)})")
            if verdict is not None or resp.stop_reason != "pause_turn":
                return VerifyResult(verdict, list(results.values()), searches, usd, model,
                                    raw_text="".join(text_parts))
            # pause_turn: o laço de busca do servidor parou no limite; reenviar
            # a resposta faz ele continuar de onde estava (sem "continue").
            # Os blocos se acumulam: cada continuação devolve só o trecho novo.
            assistant_so_far.extend(resp.content)
            messages = [{"role": "user", "content": user},
                        {"role": "assistant", "content": list(assistant_so_far)}]
        raise LLMError("verificação: excedeu o limite de continuações")
