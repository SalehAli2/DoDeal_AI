"""The HTTP speech-to-text adapters (http_stt.py) on recorded responses over
httpx.MockTransport, and the tenant's stt_profile choosing among them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.http_stt import (
    DiarizedHttpTranscriber,
    OpenAiCompatibleTranscriber,
)
from dodeal_ai.units.call_intelligence.stt import build_transcribers
from dodeal_ai.units.call_intelligence.transcriber import TranscriptionError
from dodeal_ai.units.call_intelligence.worker import transcriber_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stt"
WAV = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 120


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Engine:
    """One HTTP engine as a MockTransport handler, every request recorded."""

    def __init__(self, body: Any, status: int = 200) -> None:
        self.body, self.status = body, status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


def adapter(kind: type, engine: Engine) -> Any:
    return kind(
        base_url="http://stt.test/v1/",
        model="m1",
        api_key="test-key",
        http=httpx.AsyncClient(transport=httpx.MockTransport(engine)),
        timeout_seconds=30,
    )


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "call.wav"
    path.write_bytes(WAV)
    return path


async def test_each_adapters_recorded_answer_parses(audio: Path) -> None:
    """OpenAI's verbose_json names no voice; the owner's service names each."""
    engine = Engine(recorded("openai_verbose.json"))
    plain = await adapter(OpenAiCompatibleTranscriber, engine).transcribe(
        audio, language_hint="en", duration_seconds=150
    )
    assert [(s.speaker, s.start_s, s.end_s) for s in plain.segments] == [
        ("unknown", 0.0, 4.6),
        ("unknown", 4.6, 9.5),
    ]
    assert plain.uncertain and plain.uncertain_reasons == ("stt_no_speakers",)
    (request,) = engine.requests
    assert str(request.url) == "http://stt.test/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer test-key"
    form = request.content.decode("latin-1")
    assert 'name="response_format"\r\n\r\nverbose_json' in form
    assert 'filename="call.wav"\r\nContent-Type: audio/flac\r\n' in form
    assert 'name="language"\r\n\r\nen' in form

    owner = Engine(recorded("diarized_http.json"))
    diarized = await adapter(DiarizedHttpTranscriber, owner).transcribe(
        audio, language_hint="mixed", duration_seconds=150
    )
    assert [(s.speaker, s.language, s.confidence) for s in diarized.segments] == [
        ("speaker_1", "en", 0.93),
        ("speaker_2", "ar", None),
        ("speaker_1", "und", 0.8),
    ]
    assert diarized.uncertain_reasons == ()
    assert str(owner.requests[0].url) == "http://stt.test/v1/v1/transcriptions"


@pytest.mark.parametrize(
    ("status", "body", "reason", "retryable"),
    [
        (400, {}, "stt_request_refused", False),
        (429, {}, "stt_unavailable", True),
        (200, {"segments": "none"}, "stt_malformed_response", False),
        (302, {}, "stt_malformed_response", False),
    ],
)
async def test_a_4xx_is_permanent_and_every_failure_is_one_request(
    audio: Path, status: int, body: Any, reason: str, retryable: bool
) -> None:
    for kind in (OpenAiCompatibleTranscriber, DiarizedHttpTranscriber):
        engine = Engine(body, status)
        with pytest.raises(TranscriptionError) as caught:
            await adapter(kind, engine).transcribe(
                audio, language_hint=None, duration_seconds=150
            )
        assert (str(caught.value), caught.value.retryable) == (reason, retryable)
        assert len(engine.requests) == 1


async def test_a_dropped_connection_retries_and_no_speech_names_no_speakers(
    audio: Path,
) -> None:
    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TranscriptionError, match="^stt_unavailable$"):
        await adapter(DiarizedHttpTranscriber, dropped).transcribe(  # type: ignore[arg-type]
            audio, language_hint=None, duration_seconds=150
        )
    silent = await adapter(
        DiarizedHttpTranscriber, Engine({"segments": []})
    ).transcribe(audio, language_hint=None, duration_seconds=150)
    assert silent.uncertain_reasons == ("stt_no_speakers",)


async def test_a_tenants_stt_profile_selects_its_adapter(monkeypatch) -> None:
    """Each profile is built as its provider says; the tenant's name picks one."""
    profiles = {
        "owner": {
            "provider": "diarized_http",
            "base_url": "http://10.0.0.5:8001",
            "model": "owner-stt-1",
            "api_key_env": "OWNER_STT_KEY",
        },
        "whisper": {
            "provider": "openai_compatible",
            "base_url": "http://10.0.0.6:8000/v1",
            "model": "whisper-large-v3",
            "api_key_env": "OWNER_STT_KEY",
        },
    }
    monkeypatch.setenv("DODEAL_CALL_STT_PROVIDER", "gemini")
    monkeypatch.setenv("DODEAL_CALL_STT_API_KEY", "k1")
    monkeypatch.setenv("DODEAL_CALL_STT_PROFILES", json.dumps(profiles))
    monkeypatch.setenv("OWNER_STT_KEY", "k2")
    get_settings.cache_clear()
    async with httpx.AsyncClient() as http:
        built = build_transcribers(get_settings(), http)
        ctx = {"transcriber": built["default"], "transcribers": built}
        config = CallsConfig(stt_profile="owner")
        assert isinstance(
            transcriber_for(ctx, config.stt_profile), DiarizedHttpTranscriber
        )
        assert isinstance(transcriber_for(ctx, "whisper"), OpenAiCompatibleTranscriber)
        profiles["owner"].pop("base_url")
        monkeypatch.setenv("DODEAL_CALL_STT_PROFILES", json.dumps(profiles))
        get_settings.cache_clear()
        with pytest.raises(ConfigError, match="^stt_base_url_missing:owner$"):
            build_transcribers(get_settings(), http)
