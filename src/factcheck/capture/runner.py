"""Processo de captura: ffmpeg → nível/silêncio → backup → blocos → banco.

Roda sozinho (`factcheck capture`) porque é a única peça que não pode parar:
uma falha no painel ou numa API não pode interromper a gravação.
"""

from __future__ import annotations

import collections
import logging
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factcheck.capture.chunker import Chunk, Chunker
from factcheck.capture.levels import SilenceTracker, pcm_to_samples, rms_dbfs
from factcheck.capture.sources import (
    BYTES_PER_SAMPLE,
    SAMPLE_RATE,
    ffmpeg_file_cmd,
    ffmpeg_pulse_cmd,
)
from factcheck.capture.wavio import RotatingBackup, write_wav
from factcheck.db import transaction

log = logging.getLogger("factcheck.capture")

FRAME_S = 0.1
FRAME_BYTES = int(SAMPLE_RATE * FRAME_S) * BYTES_PER_SAMPLE
STATUS_EVERY_S = 1.0
RESTART_BACKOFF_S = (1, 2, 4, 8, 10)


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass(frozen=True)
class SourceSpec:
    kind: str                 # "pulse" | "file"
    name: str                 # nome do monitor ou caminho do arquivo
    realtime: bool = False    # só para arquivo: ler no ritmo real
    start_s: float = 0.0      # só para arquivo: começar do meio

    def command(self) -> list[str]:
        if self.kind == "pulse":
            return ffmpeg_pulse_cmd(self.name)
        return ffmpeg_file_cmd(Path(self.name), self.realtime, self.start_s)


@dataclass(frozen=True)
class CaptureParams:
    chunk_s: float
    overlap_s: float
    silence_threshold_dbfs: float
    silence_alert_s: float
    backup_part_s: float = 600.0


class CaptureRunner:
    def __init__(self, conn: sqlite3.Connection, audio_root: Path, source: SourceSpec,
                 params: CaptureParams):
        self.conn = conn
        self.source = source
        self.params = params
        self.silence = SilenceTracker(params.silence_threshold_dbfs, params.silence_alert_s)
        self.stop_event = threading.Event()
        self.run_id = self._create_run(audio_root)
        self.run_dir = audio_root / f"run_{self.run_id:04d}"
        self.backup = RotatingBackup(self.run_dir / "backup", params.backup_part_s)
        self.next_idx = 1
        self.total_samples = 0
        self._segment = 0
        self._segment_anchor: datetime | None = None
        self._segment_start_sample = 0
        self._level = -96.0
        self._last_chunk_idx: int | None = None
        self._last_chunk_at: str | None = None
        self._was_alerting = False

    # --- banco ---------------------------------------------------------------

    def _create_run(self, audio_root: Path) -> int:
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO capture_runs (source_kind, source_name, started_at, audio_dir) "
                "VALUES (?, ?, ?, '')",
                (self.source.kind, self.source.name, now_local().isoformat()),
            )
            run_id = int(cur.lastrowid)
            self.conn.execute("UPDATE capture_runs SET audio_dir = ? WHERE id = ?",
                              (str(audio_root / f"run_{run_id:04d}"), run_id))
        return run_id

    def _write_status(self, state: str, message: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO capture_status (id, run_id, state, updated_at, level_dbfs,
                   silence_seconds, silence_alert, last_chunk_idx, last_chunk_at, message)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id, state=excluded.state,
                   updated_at=excluded.updated_at, level_dbfs=excluded.level_dbfs,
                   silence_seconds=excluded.silence_seconds, silence_alert=excluded.silence_alert,
                   last_chunk_idx=excluded.last_chunk_idx, last_chunk_at=excluded.last_chunk_at,
                   message=excluded.message""",
            (self.run_id, state, now_local().isoformat(), round(self._level, 1),
             round(self.silence.silent_for_s, 1), int(self.silence.alert),
             self._last_chunk_idx, self._last_chunk_at, message),
        )

    def _on_chunk(self, chunk: Chunk) -> None:
        path = self.run_dir / "chunks" / f"chunk_{chunk.idx:05d}.wav"
        write_wav(path, chunk.pcm)
        wall_start = wall_end = None
        if self._segment_anchor is not None:
            base = self._segment_start_sample
            wall_start = (self._segment_anchor
                          + timedelta(seconds=(chunk.start_sample - base) / SAMPLE_RATE)).isoformat()
            wall_end = (self._segment_anchor
                        + timedelta(seconds=(chunk.end_sample - base) / SAMPLE_RATE)).isoformat()
        mean = rms_dbfs(pcm_to_samples(chunk.pcm))
        self.conn.execute(
            """INSERT INTO chunks (run_id, idx, segment, offset_start_s, offset_end_s,
                   wall_start, wall_end, path, mean_dbfs, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, chunk.idx, self._segment, chunk.start_s, chunk.end_s,
             wall_start, wall_end, str(path), round(mean, 1), now_local().isoformat()),
        )
        self.next_idx = chunk.idx + 1
        self._last_chunk_idx = chunk.idx
        self._last_chunk_at = now_local().isoformat()
        log.info("bloco %05d  %7.1f–%7.1f s  nível médio %5.1f dBFS",
                 chunk.idx, chunk.start_s, chunk.end_s, mean)

    # --- laço principal --------------------------------------------------------

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> str:
        """Devolve o motivo do fim: 'eof' (arquivo acabou) ou 'stopped'."""
        log.info("captura %d iniciada: %s %s", self.run_id, self.source.kind, self.source.name)
        log.info("áudio em %s", self.run_dir)
        attempt = 0
        reason = "stopped"
        try:
            while not self.stop_event.is_set():
                began = time.monotonic()
                outcome = self._run_segment()
                if outcome == "stopped" or self.source.kind == "file":
                    reason = outcome
                    break
                if time.monotonic() - began > 60:
                    attempt = 0  # caiu depois de funcionar: recomeça a espera curta
                # ffmpeg do ao vivo morreu: o som do sistema não acaba sozinho,
                # então é falha, e a captura tenta de novo sem parar o resto.
                wait = RESTART_BACKOFF_S[min(attempt, len(RESTART_BACKOFF_S) - 1)]
                attempt += 1
                msg = f"ffmpeg parou ({outcome}); nova tentativa em {wait} s"
                log.error(msg)
                self._write_status("reconectando", msg)
                if self.stop_event.wait(wait):
                    break
        finally:
            self.backup.close()
            self._write_status("parado", f"fim: {reason}")
            self.conn.execute("UPDATE capture_runs SET ended_at = ?, end_reason = ? WHERE id = ?",
                              (now_local().isoformat(), reason, self.run_id))
            log.info("captura %d encerrada (%s): %d blocos, %.1f s de áudio",
                     self.run_id, reason, self.next_idx - 1, self.total_samples / SAMPLE_RATE)
        return reason

    def _run_segment(self) -> str:
        self._segment += 1
        start_sample = (int(self.source.start_s * SAMPLE_RATE) if self.source.kind == "file"
                        else self.total_samples)
        self._segment_start_sample = start_sample
        # Arquivo não tem horário de parede: o horário no debate sai do início
        # informado pelo operador (etapa 10). No ao vivo, a âncora é a hora em
        # que o ffmpeg começou a entregar som.
        self._segment_anchor = None
        chunker = Chunker(self.params.chunk_s, self.params.overlap_s, self._on_chunk,
                          first_idx=self.next_idx, start_sample=start_sample)
        cmd = self.source.command()
        log.debug("ffmpeg: %s", " ".join(cmd))
        # Sessão própria: o Ctrl+C do terminal vai só para o Python, que
        # encerra o ffmpeg em ordem. Sem isso o ffmpeg morria primeiro e a
        # parada pedida aparecia no log como falha do ffmpeg.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        drain = threading.Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True)
        drain.start()
        self._write_status("ouvindo")
        last_status = 0.0
        outcome = "eof"
        try:
            assert proc.stdout is not None
            while True:
                if self.stop_event.is_set():
                    outcome = "stopped"
                    break
                frame = proc.stdout.read(FRAME_BYTES)
                if not frame:
                    break
                if self._segment_anchor is None and self.source.kind == "pulse":
                    self._segment_anchor = now_local() - timedelta(
                        seconds=len(frame) / BYTES_PER_SAMPLE / SAMPLE_RATE)
                n = len(frame) // BYTES_PER_SAMPLE
                self.total_samples += n
                self._level = rms_dbfs(pcm_to_samples(frame))
                self.silence.feed(self._level, n / SAMPLE_RATE)
                self.backup.write(frame)
                chunker.feed(frame)
                self._log_silence_transitions()
                mono = time.monotonic()
                if mono - last_status >= STATUS_EVERY_S:
                    last_status = mono
                    self._write_status("silencio" if self.silence.alert else "ouvindo")
        finally:
            chunker.flush()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            drain.join(timeout=2)
        if self.stop_event.is_set():
            return "stopped"
        if outcome == "eof" and proc.returncode not in (0, None) and self.source.kind == "file":
            raise RuntimeError(f"ffmpeg falhou lendo o arquivo: {' | '.join(stderr_tail)}")
        if outcome == "eof" and self.source.kind == "pulse":
            outcome = f"código {proc.returncode}: {' | '.join(stderr_tail) or 'sem mensagem'}"
        return outcome

    def _log_silence_transitions(self) -> None:
        alert = self.silence.alert
        if alert and not self._was_alerting:
            log.warning("SILÊNCIO há %.0f s — o Globoplay travou, deslogou ou está tocando em "
                        "outra saída de som?", self.silence.silent_for_s)
        elif not alert and self._was_alerting:
            log.info("som voltou")
        self._was_alerting = alert


def _drain(stream, sink: collections.deque[str]) -> None:
    # Sem ler o stderr, um ffmpeg que escreva muito erro enche o cano e trava.
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if line:
            sink.append(line)
            log.warning("ffmpeg: %s", line)
