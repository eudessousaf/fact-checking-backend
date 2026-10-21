import json
from pathlib import Path

import httpx
import pytest

from factcheck.transcription.base import SpeakerReference, TranscriptionError
from factcheck.transcription.openai_diarize import OpenAIDiarizeTranscriber, parse_segments

# Forma documentada da resposta diarized_json (a confirmar pela sondagem real).
BODY = {
    "text": "Boa noite. O desemprego caiu.",
    "duration": 30.0,
    "segments": [
        {"id": "seg_0", "type": "transcript.text.segment", "speaker": "Mediador",
         "start": 0.0, "end": 2.1, "text": " Boa noite."},
        {"id": "seg_1", "type": "transcript.text.segment", "speaker": "A",
         "start": 2.3, "end": 5.0, "text": "O desemprego caiu."},
        {"id": "seg_2", "speaker": None, "start": 5.0, "end": 5.5, "text": "   "},
    ],
    "usage": {"type": "duration", "seconds": 30},
}


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    p = tmp_path / "chunk_00001.wav"
    p.write_bytes(b"RIFF....WAVEfmt ")
    return p


def make(handler, sleeps=None):
    return OpenAIDiarizeTranscriber("sk-teste", transport=httpx.MockTransport(handler),
                                    sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_parse_drops_empty_and_marks_unknown_speaker():
    segs = parse_segments({"segments": [{"text": "oi", "start": 1, "end": 2}]})
    assert segs[0].speaker == "?"
    assert [s.text for s in parse_segments(BODY)] == ["Boa noite.", "O desemprego caiu."]


def test_request_shape_with_references(wav, tmp_path):
    ref = tmp_path / "lula.wav"
    ref.write_bytes(b"RIFFabc")
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content.decode("latin-1")
        return httpx.Response(200, json=BODY)

    t = make(handler)
    result = t.transcribe(wav, [SpeakerReference("Lula", ref)])
    assert seen["auth"] == "Bearer sk-teste"
    body = seen["body"]
    for expected in ('name="model"', "gpt-4o-transcribe-diarize", "diarized_json",
                     'name="chunking_strategy"', 'name="known_speaker_names[]"', "Lula",
                     "data:audio/wav;base64,", 'name="file"; filename="chunk_00001.wav"'):
        assert expected in body
    assert result.raw == BODY and len(result.segments) == 2


def test_bad_key_fails_fast_without_retry(wav):
    calls = []
    t = make(lambda r: calls.append(1) or httpx.Response(401, json={"error": "x"}))
    with pytest.raises(TranscriptionError, match="401"):
        t.transcribe(wav, [])
    assert len(calls) == 1


def test_no_credit_is_not_retried(wav):
    calls = []
    body = {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}
    t = make(lambda r: calls.append(1) or httpx.Response(429, json=body))
    with pytest.raises(TranscriptionError, match="sem crédito"):
        t.transcribe(wav, [])
    assert len(calls) == 1


def test_transient_errors_are_retried_then_succeed(wav):
    responses = iter([httpx.Response(503), httpx.Response(429, json={"error": "rate"}),
                      httpx.Response(200, json=BODY)])
    sleeps = []
    t = make(lambda r: next(responses), sleeps)
    assert len(t.transcribe(wav, []).segments) == 2
    assert sleeps == [2, 4]


def test_gives_up_after_all_retries(wav):
    sleeps = []
    t = make(lambda r: httpx.Response(500, text="boom"), sleeps)
    with pytest.raises(TranscriptionError, match="após tentativas"):
        t.transcribe(wav, [])
    assert sleeps == [2, 4, 8]


def test_too_many_references(wav, tmp_path):
    refs = [SpeakerReference(f"p{i}", wav) for i in range(5)]
    with pytest.raises(TranscriptionError, match="no máximo 4"):
        make(lambda r: httpx.Response(200, json=BODY)).transcribe(wav, refs)
