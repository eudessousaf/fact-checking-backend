from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class SpeakerReference:
    """Trecho curto (2–10 s) da voz de alguém conhecido, para o provedor
    reconhecer a pessoa pela voz em qualquer bloco."""
    name: str
    audio_path: Path


@dataclass(frozen=True)
class Segment:
    speaker: str          # nome de referência, ou rótulo genérico do provedor
    start_s: float        # relativo ao início do BLOCO
    end_s: float
    text: str


@dataclass
class Transcript:
    segments: list[Segment]
    raw: dict[str, Any]                 # resposta crua, guardada para depurar
    usage: dict[str, Any] = field(default_factory=dict)
    duration_s: float | None = None


class TranscriptionError(RuntimeError):
    """Falha que não adianta repetir (chave errada, pedido inválido)."""


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path, references: list[SpeakerReference]) -> Transcript: ...
