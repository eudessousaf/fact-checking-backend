"""Processo do fluxo (`factcheck pipeline`): roda os estágios em laço.

Separado da captura de propósito: se uma API travar ou este processo cair, a
captura continua gravando, e ao voltar ele retoma do primeiro bloco pendente.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from factcheck.config import ConfigError, DebateConfig, Settings
from factcheck.costs import TokenPrice
from factcheck.db import connect
from factcheck.pipeline import transcribe
from factcheck.transcription.base import Transcriber, TranscriptionError

log = logging.getLogger("factcheck.pipeline")

IDLE_WAIT_S = 1.0


def check_voices(debate: DebateConfig) -> None:
    missing = [r for r in ("A", "B") if r not in debate.voices or not debate.voices[r].exists()]
    if missing:
        raise ConfigError(
            "faltam as vozes de referência dos candidatos "
            f"({', '.join(missing)}) em [voices] do config/debate.toml. Sem elas os rótulos "
            "trocam entre blocos (medido em 22/10) e a transcrição não serve para nada."
        )
    if "M" not in debate.voices or not debate.voices["M"].exists():
        log.warning("sem a voz do MEDIADOR: as falas dele podem sair com o nome de um candidato. "
                    "Elas ficam presas na fila, mas grave a voz antes do debate.")


class TranscribeWorkers:
    def __init__(self, settings: Settings, debate: DebateConfig,
                 make_transcriber: Callable[[], Transcriber], workers: int,
                 provider: str = "openai"):
        check_voices(debate)
        self.settings = settings
        self.refs = transcribe.references_for(debate.voices)
        self.make_transcriber = make_transcriber
        self.workers = max(1, workers)
        self.provider = provider
        self.price = TokenPrice(settings.transcription_price_in_per_m, settings.transcription_price_out_per_m)

    def _loop(self, stop: threading.Event) -> None:
        conn = connect(self.settings.db_path)
        transcriber = self.make_transcriber()
        try:
            while not stop.is_set():
                chunk = transcribe.claim_next(conn)
                if chunk is None:
                    stop.wait(IDLE_WAIT_S)
                    continue
                try:
                    n = transcribe.transcribe_chunk(
                        conn, chunk, transcriber, self.refs, self.settings.chunk_overlap_seconds,
                        self.price, self.provider, self.settings.transcription_model)
                    log.info("bloco %05d (captura %d) transcrito: %d falas", chunk["idx"], chunk["run_id"], n)
                except TranscriptionError as exc:
                    transcribe.fail_chunk(conn, chunk, str(exc))
                except Exception as exc:  # um bloco com defeito não derruba o fluxo
                    log.exception("erro inesperado no bloco %s", chunk["idx"])
                    transcribe.fail_chunk(conn, chunk, f"erro inesperado: {exc}")
        finally:
            close = getattr(transcriber, "close", None)
            if close:
                close()
            conn.close()

    def start(self, stop: threading.Event) -> list[threading.Thread]:
        conn = connect(self.settings.db_path)
        stale = transcribe.reset_stale(conn)
        conn.close()
        if stale:
            log.info("%d bloco(s) interrompido(s) por uma queda voltaram para a fila", stale)
        threads = [threading.Thread(target=self._loop, args=(stop,), name=f"transcricao-{i}", daemon=True)
                   for i in range(self.workers)]
        for t in threads:
            t.start()
        log.info("transcrição: %d trabalhador(es), vozes: %s", self.workers,
                 ", ".join(r.name.removeprefix(transcribe.REF_PREFIX) for r in self.refs))
        return threads


# --- análise (intervalo + detecção), verificação e publicação --------------------------

LLM_RETRY_WAITS_S = (5, 15)


def _with_retries(stop: threading.Event, what: str, fn):
    """Erro de API passageiro não pode custar um bloco: tenta de novo antes de desistir."""
    from factcheck.llm.claude import LLMError

    last: LLMError | None = None
    for wait in (*LLM_RETRY_WAITS_S, None):
        try:
            return fn()
        except LLMError as exc:
            last = exc
            if wait is None or stop.is_set():
                break
            log.warning("%s falhou (%s); nova tentativa em %d s", what, exc, wait)
            stop.wait(wait)
    assert last is not None
    raise last


class AnalysisWorker:
    """Uma linha só, em ordem: a regra do intervalo depende do bloco anterior."""

    def __init__(self, settings: Settings, llm):
        self.settings = settings
        self.llm = llm

    def _loop(self, stop: threading.Event) -> None:
        from factcheck.llm.claude import LLMError
        from factcheck.pipeline import detect, interval

        conn = connect(self.settings.db_path)
        try:
            while not stop.is_set():
                worked = False
                chunk = interval.next_pending(conn)
                if chunk is not None:
                    worked = True
                    try:
                        _with_retries(stop, f"intervalo do bloco {chunk['idx']}",
                                      lambda: interval.classify_one(conn, chunk, self.llm))
                    except LLMError as exc:
                        interval.classify_failed(conn, chunk, str(exc))
                    except Exception as exc:
                        log.exception("erro inesperado classificando o bloco %s", chunk["idx"])
                        interval.classify_failed(conn, chunk, f"erro inesperado: {exc}")
                chunk = detect.next_pending(conn)
                if chunk is not None:
                    worked = True
                    try:
                        _with_retries(stop, f"detecção do bloco {chunk['idx']}",
                                      lambda: detect.detect_one(conn, chunk, self.llm))
                    except LLMError as exc:
                        detect.mark_failed(conn, chunk, str(exc))
                    except Exception as exc:
                        log.exception("erro inesperado detectando no bloco %s", chunk["idx"])
                        detect.mark_failed(conn, chunk, f"erro inesperado: {exc}")
                if not worked:
                    stop.wait(IDLE_WAIT_S)
        finally:
            conn.close()

    def start(self, stop: threading.Event) -> list[threading.Thread]:
        t = threading.Thread(target=self._loop, args=(stop,), name="analise", daemon=True)
        t.start()
        return [t]


class VerifyWorkers:
    def __init__(self, settings: Settings, debate: DebateConfig, verifier, workers: int):
        self.settings = settings
        self.debate = debate
        self.verifier = verifier
        self.workers = max(1, workers)
        self._budget_logged = False

    def _debate_date(self) -> str:
        from factcheck.capture.runner import now_local

        return (self.debate.scheduled_start or now_local().isoformat())[:10]

    def _loop(self, stop: threading.Event) -> None:
        from factcheck.costs import total_usd
        from factcheck.llm.claude import LLMError
        from factcheck.pipeline import verify

        conn = connect(self.settings.db_path)
        try:
            while not stop.is_set():
                spent = total_usd(conn, "anthropic")
                if spent >= self.settings.budget_hard_stop_usd:
                    if not self._budget_logged:
                        log.error("TETO DE US$ %.0f ATINGIDO (gasto: US$ %.2f): novas checagens paradas",
                                  self.settings.budget_hard_stop_usd, spent)
                        self._budget_logged = True
                    stop.wait(10)
                    continue
                claim = verify.claim_next(conn)
                if claim is None:
                    stop.wait(IDLE_WAIT_S)
                    continue
                try:
                    verify.verify_one(conn, claim, self.verifier, self._debate_date(), self.debate.veto_minutes)
                except LLMError as exc:
                    verify.fail_claim(conn, claim, str(exc))
                except Exception as exc:
                    log.exception("erro inesperado verificando a afirmação %s", claim["id"])
                    verify.fail_claim(conn, claim, f"erro inesperado: {exc}")
        finally:
            conn.close()

    def start(self, stop: threading.Event) -> list[threading.Thread]:
        from factcheck.pipeline import verify

        conn = connect(self.settings.db_path)
        stale = verify.reset_stale(conn)
        conn.close()
        if stale:
            log.info("%d verificação(ões) interrompida(s) voltaram para a fila", stale)
        threads = [threading.Thread(target=self._loop, args=(stop,), name=f"verificacao-{i}", daemon=True)
                   for i in range(self.workers)]
        for t in threads:
            t.start()
        return threads


class PublishLoop:
    INTERVAL_S = 2.0

    def __init__(self, settings: Settings, publisher):
        self.settings = settings
        self.publisher = publisher

    def _loop(self, stop: threading.Event) -> None:
        from factcheck.contract import ContractError
        from factcheck.pipeline import publish

        conn = connect(self.settings.db_path)
        try:
            while not stop.is_set():
                try:
                    publish.publish_due(conn)
                    self.publisher.sync(conn)
                except ContractError as exc:
                    log.error("timeline.json NÃO enviado (fora do contrato): %s", "; ".join(exc.problems))
                except Exception:
                    # Falha de rede no envio: o arquivo anterior segue no ar e a
                    # próxima volta tenta de novo.
                    log.exception("falha ao publicar; tentando de novo em %.0f s", self.INTERVAL_S)
                    self.publisher.last_hash = None
                stop.wait(self.INTERVAL_S)
        finally:
            conn.close()

    def start(self, stop: threading.Event) -> list[threading.Thread]:
        t = threading.Thread(target=self._loop, args=(stop,), name="publicacao", daemon=True)
        t.start()
        return [t]
