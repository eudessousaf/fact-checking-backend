"""Corte do fluxo contínuo em blocos de ~30 s com ~2 s de sobreposição.

A sobreposição existe para nenhuma palavra cair exatamente no corte e sumir
dos dois blocos. O custo é que os 2 s aparecem duas vezes na transcrição, e
quem remove a duplicata é a etapa de transcrição (pelos tempos absolutos).

Todos os tempos são contados em AMOSTRAS, não no relógio: o relógio do
computador e o do áudio andam separados, e o horário de cada fala no debate
precisa sair do áudio.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from factcheck.capture.sources import BYTES_PER_SAMPLE, SAMPLE_RATE


@dataclass(frozen=True)
class Chunk:
    idx: int
    start_sample: int
    end_sample: int
    pcm: bytes

    @property
    def start_s(self) -> float:
        return self.start_sample / SAMPLE_RATE

    @property
    def end_s(self) -> float:
        return self.end_sample / SAMPLE_RATE


class Chunker:
    def __init__(self, chunk_s: float, overlap_s: float, on_chunk: Callable[[Chunk], None],
                 first_idx: int = 1, start_sample: int = 0, min_final_s: float = 1.0):
        self.chunk_samples = int(chunk_s * SAMPLE_RATE)
        self.overlap_samples = int(overlap_s * SAMPLE_RATE)
        if not (0 <= self.overlap_samples < self.chunk_samples):
            raise ValueError("sobreposição precisa ser menor que o bloco")
        self.on_chunk = on_chunk
        self.next_idx = first_idx
        self.min_final_samples = int(min_final_s * SAMPLE_RATE)
        self._buf = bytearray()
        # Amostra em que _buf começa. Não é zero quando um arquivo é retomado
        # do meio (pós-debate): o deslocamento continua contado desde o início
        # do arquivo, e os horários das falas continuam certos.
        self._buf_start = start_sample
        self._new_since_emit = 0  # amostras ainda não entregues em nenhum bloco

    def feed(self, pcm: bytes) -> None:
        self._buf += pcm
        self._new_since_emit += len(pcm) // BYTES_PER_SAMPLE
        chunk_bytes = self.chunk_samples * BYTES_PER_SAMPLE
        while len(self._buf) >= chunk_bytes:
            self._emit(bytes(self._buf[:chunk_bytes]))
            step = (self.chunk_samples - self.overlap_samples) * BYTES_PER_SAMPLE
            del self._buf[:step]
            self._buf_start += self.chunk_samples - self.overlap_samples
            self._new_since_emit = len(self._buf) // BYTES_PER_SAMPLE - self.overlap_samples

    def flush(self) -> None:
        """Entrega o resto no fim do fluxo, se houver áudio novo suficiente.

        O que sobra no buffer começa com a sobreposição do bloco anterior;
        só ela não justifica um bloco a mais.
        """
        if self._new_since_emit >= self.min_final_samples and self._buf:
            self._emit(bytes(self._buf))
        self._buf.clear()
        self._new_since_emit = 0

    def _emit(self, pcm: bytes) -> None:
        n = len(pcm) // BYTES_PER_SAMPLE
        chunk = Chunk(self.next_idx, self._buf_start, self._buf_start + n, pcm)
        self.next_idx += 1
        self.on_chunk(chunk)
