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
