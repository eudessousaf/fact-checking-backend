"""OpenAI `gpt-4o-transcribe-diarize`: transcrição com falante por voz.

Por que este provedor (escolhido pelo Eudes em 21/10/2026): os blocos são
transcritos um a um, e só ele reconhece a MESMA pessoa em blocos diferentes —
pelos `known_speaker_references`. Os outros devolvem "Falante 1" sem garantia
de que seja o mesmo "Falante 1" do bloco anterior.

O que vem de documentação (lida por busca, não direto — a rede da nuvem não
alcança a OpenAI) e precisa ser CONFIRMADO pela sondagem na máquina do
operador: `response_format=diarized_json`, `chunking_strategy` obrigatório
acima de 30 s, até 4 referências de 2–10 s em data URL, e segmentos com
`speaker`, `start`, `end`, `text`. Por isso o parser é tolerante e a
resposta crua é sempre guardada.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from factcheck.transcription.base import (
    Segment,
    SpeakerReference,
    Transcript,
    TranscriptionError,
)

log = logging.getLogger("factcheck.transcription")

API_URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_REFERENCES = 4
RETRY_WAITS_S = (2, 4, 8)


def _data_url(path: Path) -> str:
    mime = "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def parse_segments(body: dict[str, Any]) -> list[Segment]:
    segments = []
    for raw in body.get("segments") or []:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        try:
            start, end = float(raw.get("start", 0.0)), float(raw.get("end", 0.0))
        except (TypeError, ValueError):
            start = end = 0.0
        # Sem falante é "?", nunca um palpite: fala sem dono não é publicada.
        speaker = str(raw.get("speaker") or "?").strip() or "?"
        segments.append(Segment(speaker=speaker, start_s=start, end_s=end, text=text))
    return segments


class OpenAIDiarizeTranscriber:
    def __init__(self, api_key: str, model: str = "gpt-4o-transcribe-diarize",
                 language: str | None = "pt", timeout_s: float = 120.0,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        if not api_key:
            raise TranscriptionError("OPENAI_API_KEY vazio no .env")
        self.model = model
        self.language = language
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout_s, transport=transport,
                                    headers={"Authorization": f"Bearer {api_key}"})

    def _form(self, references: list[SpeakerReference]) -> dict[str, str | list[str]]:
        form: dict[str, str | list[str]] = {
            "model": self.model, "response_format": "diarized_json", "chunking_strategy": "auto"}
        if self.language:
            form["language"] = self.language
        if len(references) > MAX_REFERENCES:
            raise TranscriptionError(f"no máximo {MAX_REFERENCES} vozes de referência")
        if references:
            # Campo repetido no multipart: nomes e áudios na MESMA ordem.
            form["known_speaker_names[]"] = [r.name for r in references]
            form["known_speaker_references[]"] = [_data_url(r.audio_path) for r in references]
        return form

    def transcribe(self, audio_path: Path, references: list[SpeakerReference]) -> Transcript:
        form = self._form(references)
        last_error = ""
        for attempt in range(len(RETRY_WAITS_S) + 1):
            try:
                with audio_path.open("rb") as fh:
                    resp = self._client.post(API_URL, data=form,
                                             files={"file": (audio_path.name, fh, "audio/wav")})
            except httpx.TransportError as exc:
                last_error = f"rede: {exc}"
            else:
                if resp.status_code == 200:
                    body = resp.json()
                    return Transcript(segments=parse_segments(body), raw=body,
                                      usage=body.get("usage") or {},
                                      duration_s=body.get("duration"))
                detail = resp.text[:500]
                if resp.status_code == 401:
                    raise TranscriptionError("chave da OpenAI recusada (401): confira OPENAI_API_KEY")
                if resp.status_code == 429 and "insufficient_quota" in detail:
                    # Sem crédito não é congestionamento: esperar não resolve.
                    raise TranscriptionError("a conta da OpenAI está sem crédito (insufficient_quota)")
                if resp.status_code in (400, 403, 404, 413, 415, 422):
                    # Repetir não conserta pedido errado — e gastaria crédito.
                    raise TranscriptionError(f"OpenAI recusou o pedido ({resp.status_code}): {detail}")
                last_error = f"HTTP {resp.status_code}: {detail}"
            if attempt < len(RETRY_WAITS_S):
                wait = RETRY_WAITS_S[attempt]
                log.warning("transcrição de %s falhou (%s); nova tentativa em %d s",
                            audio_path.name, last_error, wait)
                self._sleep(wait)
        raise TranscriptionError(f"transcrição de {audio_path.name} falhou após tentativas: {last_error}")

    def close(self) -> None:
        self._client.close()
