"""GeminiTranscriber on recorded responses, through the real google-genai SDK
over httpx.MockTransport: never a live call."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.units.call_intelligence import gemini
from dodeal_ai.units.call_intelligence.gemini import (
    MALFORMED,
    REFUSED,
    UNAVAILABLE,
    UNKNOWN_FORMAT,
    GeminiTranscriber,
    audio_mime,
    failure,
)
from dodeal_ai.units.call_intelligence.stt import build_transcribers
from dodeal_ai.units.call_intelligence.transcriber import TranscriptionError
from dodeal_ai.units.call_intelligence.worker import transcriber_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stt"
RECORDED: dict[str, Any] = json.loads(
    (FIXTURES / "gemini_interaction.json").read_text(encoding="utf-8")
)
WAV = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 120
UPLOADED = {"name": "files/fixture-1", "uri": "https://files.test/fixture-1"}


class Google:
    """Google's API as a MockTransport handler: every request recorded, the
    interaction answered with `answer` or `status`, the upload with its own."""

    def __init__(
        self,
        answer: dict[str, Any] = RECORDED,
        *,
        status: int = 200,
        upload_status: int = 200,
        delete_status: int = 200,
    ) -> None:
        self.answer, self.status = answer, status
        self.upload_status, self.delete_status = upload_status, delete_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.startswith("/upload/"):
            return self._reply(
                self.upload_status,
                {"file": {**UPLOADED, "mimeType": "audio/wav"}},
                headers={
                    "x-goog-upload-status": "final",
                    "x-goog-upload-url": "https://generativelanguage.googleapis.com/upload/x",
                },
            )
        if request.method == "DELETE":
            return self._reply(self.delete_status, {})
        return self._reply(self.status, self.answer)

    @staticmethod
    def _reply(
        status: int, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if status != 200:
            body = {"error": {"code": status, "message": "provider words"}}
        return httpx.Response(status, json=body, headers=headers)

    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


def transcriber(google: Google) -> GeminiTranscriber:
    return GeminiTranscriber(
        model="gemini-3.5-transcribe",
        api_key="test-key",
        http=httpx.AsyncClient(transport=httpx.MockTransport(google)),
        base_url=None,
        timeout_seconds=30,
    )


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "call.audio"
    path.write_bytes(WAV)
    return path


async def test_spk_1_and_spk_2_come_back_as_ordered_speaker_segments(
    audio: Path,
) -> None:
    """Diarized words become speaker turns, in order, in one request."""
    google = Google()
    transcript = await transcriber(google).transcribe(audio, language_hint="mixed")
    assert [(s.speaker, s.language, s.confidence) for s in transcript.segments] == [
        ("speaker_1", "en", None),
        ("speaker_2", "ar", None),
        ("speaker_1", "und", None),
        ("speaker_2", "en", None),
    ]
    starts = [(s.start_s, s.end_s) for s in transcript.segments]
    assert starts == [(0.1, 2.4), (2.4, 4.0), (4.2, 4.9), (5.0, 5.9)]
    assert transcript.segments[1].text == "مرحبا أريد شقة."
    assert (transcript.provider, transcript.model) == (
        "gemini",
        "gemini-3.5-transcribe",
    )
    (request,) = google.requests
    assert request.url.path == "/v1beta/interactions"
    assert request.headers["x-goog-api-key"] == "test-key"
    body = json.loads(request.content)
    config = body["generation_config"]["transcription_config"]
    assert config == {
        "mode": {
            "type": "verbatim",
            "diarization_mode": "speaker",
            "timestamp_granularities": ["word"],
        },
        "language_codes": ["ar", "en"],
    }
    (audio_input,) = body["input"][0]["content"]
    assert audio_input["mime_type"] == "audio/wav"


async def test_the_upload_is_deleted_even_when_the_transcription_fails(
    audio: Path, monkeypatch
) -> None:
    """A large file goes through the Files API and is deleted in a finally."""
    monkeypatch.setattr(gemini, "INLINE_MAX_BYTES", 8)
    google = Google(status=400)
    with pytest.raises(TranscriptionError) as caught:
        await transcriber(google).transcribe(audio, language_hint=None)
    assert (str(caught.value), caught.value.retryable) == (REFUSED, False)
    assert google.paths()[-2:] == [
        ("POST", "/v1beta/interactions"),
        ("DELETE", "/v1beta/files/fixture-1"),
    ]
    (sent,) = json.loads(google.requests[-2].content)["input"][0]["content"]
    assert sent["uri"] == UPLOADED["uri"] and "data" not in sent


@pytest.mark.parametrize("status", [503, 429])
async def test_a_503_or_a_429_makes_exactly_one_request_on_either_layer(
    audio: Path, monkeypatch, status: int
) -> None:
    """The SDK's own retries are off: the worker's one retry is the only one."""
    google = Google(status=status)
    with pytest.raises(TranscriptionError) as caught:
        await transcriber(google).transcribe(audio, language_hint=None)
    assert (str(caught.value), caught.value.retryable) == (UNAVAILABLE, True)
    assert google.paths() == [("POST", "/v1beta/interactions")]

    monkeypatch.setattr(gemini, "INLINE_MAX_BYTES", 8)
    uploads = Google(upload_status=status)
    with pytest.raises(TranscriptionError, match=f"^{UNAVAILABLE}$"):
        await transcriber(uploads).transcribe(audio, language_hint=None)
    assert uploads.paths() == [("POST", "/upload/v1beta/files")]


async def test_failures_are_classified_by_status_and_never_carry_its_words(
    audio: Path, tmp_path: Path
) -> None:
    """408 and a dropped connection retry; other 4xx, bad answers never do."""
    assert failure(gemini.genai_errors.APIError(302, {})).reason == MALFORMED
    for google, reason, retryable in [
        (Google(status=408), UNAVAILABLE, True),
        (Google(status=404), REFUSED, False),
        (Google({"steps": [{"type": "model_output", "content": [
            {"type": "text", "text": "words", "annotations": []}]}]}), MALFORMED, False),
        (Google({"steps": [{"type": "model_output", "content": [
            {"type": "text", "text": "w", "annotations": [
                {"type": "word_info", "text": "w", "start_offset": "1s", "end_offset": "2s"}
            ]}]}]}), MALFORMED, False),
    ]:  # fmt: skip
        with pytest.raises(TranscriptionError) as caught:
            await transcriber(google).transcribe(audio, language_hint="en")
        assert (str(caught.value), caught.value.retryable) == (reason, retryable)
        assert "provider words" not in repr(caught.value)

    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(TranscriptionError, match=f"^{UNAVAILABLE}$"):
        await transcriber(dropped).transcribe(audio, language_hint=None)  # type: ignore[arg-type]
    unknown = tmp_path / "call.m4a"
    unknown.write_bytes(b"\x00\x00\x00\x20ftypM4A ")
    silent = Google()
    with pytest.raises(TranscriptionError, match=f"^{UNKNOWN_FORMAT}$"):
        await transcriber(silent).transcribe(unknown, language_hint=None)
    assert silent.requests == []
    assert [audio_mime(head) for head in (b"\xff\xf1", b"\xff\xfb", b"ID3", b"x")] == [
        "audio/aac",
        "audio/mp3",
        "audio/mp3",
        None,
    ]


async def test_a_failed_delete_is_logged_by_type_and_the_call_still_succeeds(
    audio: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(gemini, "INLINE_MAX_BYTES", 8)
    with caplog.at_level(logging.ERROR, logger="dodeal_ai.calls.stt"):
        transcript = await transcriber(Google(delete_status=500)).transcribe(
            audio, language_hint=None
        )
    assert transcript.speakers == ("speaker_1", "speaker_2")
    (record,) = caplog.records
    assert (record.getMessage(), record.error_type) == (  # type: ignore[attr-defined]
        "stt_upload_delete_failed",
        "ServerError",
    )


async def test_the_factory_builds_gemini_per_profile_and_reads_each_key_once(
    monkeypatch,
) -> None:
    """The default's key from CALL_STT_API_KEY, a named one's from its variable."""
    monkeypatch.setenv("DODEAL_CALL_STT_PROVIDER", "gemini")
    profile = {"provider": "gemini", "model": "m2", "api_key_env": "OWNER_STT_KEY"}
    monkeypatch.setenv("DODEAL_CALL_STT_PROFILES", json.dumps({"second": profile}))
    get_settings.cache_clear()
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConfigError, match="^stt_api_key_missing:default$"):
            build_transcribers(get_settings(), http)
        monkeypatch.setenv("DODEAL_CALL_STT_API_KEY", "k1")
        get_settings.cache_clear()
        with pytest.raises(ConfigError, match="^stt_api_key_missing:second$"):
            build_transcribers(get_settings(), http)
        monkeypatch.setenv("OWNER_STT_KEY", "k2")
        built = build_transcribers(get_settings(), http)
        assert sorted(built) == ["default", "second"]
        assert all(isinstance(t, GeminiTranscriber) for t in built.values())
        monkeypatch.setenv("DODEAL_CALL_STT_PROVIDER", "fake")
        get_settings.cache_clear()
        with pytest.raises(ConfigError, match="^stt_not_configured$"):
            build_transcribers(get_settings(), http)


def test_a_tenant_profile_the_worker_has_no_engine_for_fails_the_call() -> None:
    """Never a fallback to another engine: the call fails, unpaid."""
    default = object()
    ctx = {"transcriber": default, "transcribers": {"default": default}}
    assert transcriber_for(ctx, "default") is default
    with pytest.raises(TranscriptionError, match="^stt_profile_not_configured$"):
        transcriber_for(ctx, "second")
