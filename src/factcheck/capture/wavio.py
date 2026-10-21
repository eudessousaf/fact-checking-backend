"""Escrita de WAV mono 16 kHz — formato que todo provedor de transcrição aceita."""

from __future__ import annotations

import wave
from pathlib import Path

from factcheck.capture.sources import BYTES_PER_SAMPLE, CHANNELS, SAMPLE_RATE


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    # Renomear é atômico: quem lê o diretório nunca pega um bloco pela metade.
    tmp.replace(path)


class RotatingBackup:
    """Gravação completa em partes de N minutos.

    O módulo wave corrige o cabeçalho a cada escrita, então uma queda não
    zera o arquivo. As partes existem por outro motivo: um defeito no disco
    perde no máximo uma parte, e a retomada pós-debate (etapa 10) abre a
    parte certa em vez de varrer duas horas de áudio.
    """

    def __init__(self, directory: Path, part_seconds: float = 600.0):
        self.directory = directory
        self.part_bytes = int(part_seconds * SAMPLE_RATE) * BYTES_PER_SAMPLE
        self.part_no = 0
        self._writer: wave.Wave_write | None = None
        self._written = 0
        directory.mkdir(parents=True, exist_ok=True)

    def _open_next(self) -> None:
        self.close()
        self.part_no += 1
        path = self.directory / f"backup_{self.part_no:04d}.wav"
        w = wave.open(str(path), "wb")
        w.setnchannels(CHANNELS)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(SAMPLE_RATE)
        self._writer = w
        self._written = 0

    def write(self, pcm: bytes) -> None:
        while pcm:
            if self._writer is None or self._written >= self.part_bytes:
                self._open_next()
            assert self._writer is not None
            room = self.part_bytes - self._written
            piece, pcm = pcm[:room], pcm[room:]
            self._writer.writeframes(piece)
            self._written += len(piece)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
