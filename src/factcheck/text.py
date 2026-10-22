"""Normalização de texto para comparar frases (sem acento, sem caixa, sem pontuação)."""

from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SPACES.sub(" ", _NON_WORD.sub(" ", ascii_text)).strip()
