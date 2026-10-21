"""Nível de áudio em dBFS (decibéis em relação ao máximo digital: 0 é o teto,
-90 é praticamente silêncio) e detecção de silêncio prolongado."""

from __future__ import annotations

import math
import sys
from array import array

FLOOR_DBFS = -96.0


def pcm_to_samples(pcm: bytes) -> array:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":  # o ffmpeg entrega little-endian (s16le)
        samples.byteswap()
    return samples


def rms_dbfs(samples: array) -> float:
    if len(samples) == 0:
        return FLOOR_DBFS
    mean_sq = math.fsum(s * s for s in samples) / len(samples)
    if mean_sq <= 0:
        return FLOOR_DBFS
    return max(FLOOR_DBFS, 20 * math.log10(math.sqrt(mean_sq) / 32768.0))


class SilenceTracker:
    """Conta há quanto tempo o áudio está abaixo do limiar.

    Silêncio longo quase sempre é o Globoplay travado, deslogado ou tocando
    em outra saída de som — não um debate em pausa, que tem som de estúdio.
    """

    def __init__(self, threshold_dbfs: float, alert_after_s: float):
        self.threshold_dbfs = threshold_dbfs
        self.alert_after_s = alert_after_s
        self.silent_for_s = 0.0

    def feed(self, level_dbfs: float, duration_s: float) -> None:
        if level_dbfs < self.threshold_dbfs:
            self.silent_for_s += duration_s
        else:
            self.silent_for_s = 0.0

    @property
    def alert(self) -> bool:
        return self.silent_for_s >= self.alert_after_s
