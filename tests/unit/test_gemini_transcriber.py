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
    GeminiTranscriber,
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
    transcript = await transcriber(google).transcribe(
        audio, language_hint="mixed", duration_seconds=150
    )
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
    # Whatever the file, the worker converted it: FLAC is what is declared.
    assert audio_input["mime_type"] == "audio/flac"


def _live(words: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    """An answer in the shape the live API sent on 2026-09-25: word_info
    annotations with spk:N speakers. The words are made up."""
    notes = [
        {
            "type": "word_info",
            "text": text,
            "speaker": speaker,
            "start_offset": start,
            "end_offset": end,
        }
        for text, speaker, start, end in words
    ]
    text = " ".join(word[0] for word in words)
    return {
        "id": "interaction-live-shape",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": text, "annotations": notes}],
            }
        ],
    }


async def test_the_live_spk_colon_labels_come_back_as_speaker_0_and_1(
    audio: Path,
) -> None:
    """The live API's spk:0 and spk:1 become speaker_0 and speaker_1."""
    google = Google(
        _live(
            [
                ("Good", "spk:0", "0.000s", "0.400s"),
                ("morning.", "spk:0", "0.400s", "1.900s"),
                ("Hi,", "spk:1", "1.900s", "2.300s"),
                ("yes.", "spk:1", "2.300s", "2.800s"),
                ("Great.", "spk:0", "3.000s", "3.500s"),
            ]
        )
    )
    transcript = await transcriber(google).transcribe(
        audio, language_hint="en", duration_seconds=60
    )
    assert [(s.speaker, s.start_s, s.end_s, s.text) for s in transcript.segments] == [
        ("speaker_0", 0.0, 1.9, "Good morning."),
        ("speaker_1", 1.9, 2.8, "Hi, yes."),
        ("speaker_0", 3.0, 3.5, "Great."),
    ]


async def test_the_documented_spk_underscore_label_still_parses(audio: Path) -> None:
    """The documented spk_1 form keeps working beside the live one."""
    google = Google(_live([("Hello.", "spk_1", "0.100s", "0.900s")]))
    transcript = await transcriber(google).transcribe(
        audio, language_hint="en", duration_seconds=60
    )
    assert [s.speaker for s in transcript.segments] == ["speaker_1"]


@pytest.mark.parametrize("speaker", ["spk-1", "spk:", "spk::1", "spk:1234", "SPK:1"])
async def test_a_speaker_in_neither_form_is_malformed(
    audio: Path, speaker: str
) -> None:
    google = Google(_live([("Hello.", speaker, "0.100s", "0.900s")]))
    with pytest.raises(TranscriptionError) as caught:
        await transcriber(google).transcribe(
            audio, language_hint="en", duration_seconds=60
        )
    assert caught.value.reason == MALFORMED


def _undiarized(answer: dict[str, Any]) -> dict[str, Any]:
    """The recorded answer as Gemini gives it with no diarization asked: the
    same words, none carrying a speaker."""
    stripped = json.loads(json.dumps(answer))
    for step in stripped["steps"]:
        for part in step.get("content") or ():
            for note in part.get("annotations") or ():
                note.pop("speaker", None)
    return stripped


@pytest.mark.parametrize(("seconds", "diarized"), [(1800, True), (1801, False)])
async def test_a_call_over_1800_seconds_is_sent_without_diarization(
    audio: Path, seconds: int, diarized: bool
) -> None:
    """The guard (F-4): 1801 s makes a request without diarization, its
    speakers unknown and the transcript uncertain; 1800 s keeps it."""
    google = Google(RECORDED if diarized else _undiarized(RECORDED))
    transcript = await transcriber(google).transcribe(
        audio, language_hint=None, duration_seconds=seconds
    )
    (request,) = google.requests
    mode = json.loads(request.content)["generation_config"]["transcription_config"][
        "mode"
    ]
    expected = {"type": "verbatim", "timestamp_granularities": ["word"]}
    if diarized:
        assert mode == {**expected, "diarization_mode": "speaker"}
        assert transcript.speakers == ("speaker_1", "speaker_2")
        assert transcript.uncertain_reasons == ()
        return
    assert mode == expected
    assert [
        (s.speaker, s.start_s, s.end_s, s.language) for s in transcript.segments
    ] == [
        ("unknown", 0.1, 2.4, "en"),
        ("unknown", 2.4, 4.0, "ar"),
        ("unknown", 4.2, 5.9, "en"),
    ]
    assert transcript.segments[2].text == "2025 Great, when?"
    assert transcript.uncertain
    assert transcript.uncertain_reasons == ("long_call_no_diarization",)


async def test_a_diarized_answer_missing_a_speaker_is_malformed(audio: Path) -> None:
    """Diarization was asked for: a word with no speaker is never unknown."""
    google = Google(_undiarized(RECORDED))
    with pytest.raises(TranscriptionError, match=f"^{MALFORMED}$"):
        await transcriber(google).transcribe(
            audio, language_hint=None, duration_seconds=150
        )
    assert len(google.requests) == 1


@pytest.mark.parametrize("seconds", [150, 2400])
async def test_a_word_whose_times_cannot_be_read_is_malformed(
    audio: Path, seconds: int
) -> None:
    """Diarized or not, a word's offsets must be Gemini's "<n>s": anything
    else is an answer that cannot be read, never asked again."""
    answer = json.loads(json.dumps(RECORDED))
    (part,) = answer["steps"][-1]["content"]
    part["annotations"][0]["start_offset"] = "0.1"
    google = Google(answer if seconds <= 1800 else _undiarized(answer))
    with pytest.raises(TranscriptionError, match=f"^{MALFORMED}$") as caught:
        await transcriber(google).transcribe(
            audio, language_hint=None, duration_seconds=seconds
        )
    assert caught.value.retryable is False
    assert len(google.requests) == 1


async def test_a_pause_ends_an_undiarized_segment(audio: Path) -> None:
    """With no sentence's end between them, a pause of 1 s still cuts."""
    answer = _undiarized(RECORDED)
    (part,) = answer["steps"][-1]["content"]
    part["annotations"] = [
        {
            "type": "word_info",
            "text": "one",
            "start_offset": "0.0s",
            "end_offset": "0.5s",
        },
        {
            "type": "word_info",
            "text": "two",
            "start_offset": "1.4s",
            "end_offset": "1.8s",
        },
        {
            "type": "word_info",
            "text": "three",
            "start_offset": "2.8s",
            "end_offset": "3.1s",
        },
    ]
    transcript = await transcriber(Google(answer)).transcribe(
        audio, language_hint="en", duration_seconds=2400
    )
    assert [s.text for s in transcript.segments] == ["one two", "three"]


async def test_the_upload_is_deleted_even_when_the_transcription_fails(
    audio: Path, monkeypatch
) -> None:
    """A large file goes through the Files API and is deleted in a finally."""
    monkeypatch.setattr(gemini, "INLINE_MAX_BYTES", 8)
    google = Google(status=400)
    with pytest.raises(TranscriptionError) as caught:
        await transcriber(google).transcribe(
            audio, language_hint=None, duration_seconds=150
        )
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
        await transcriber(google).transcribe(
            audio, language_hint=None, duration_seconds=150
        )
    assert (str(caught.value), caught.value.retryable) == (UNAVAILABLE, True)
    assert google.paths() == [("POST", "/v1beta/interactions")]

    monkeypatch.setattr(gemini, "INLINE_MAX_BYTES", 8)
    uploads = Google(upload_status=status)
    with pytest.raises(TranscriptionError, match=f"^{UNAVAILABLE}$"):
        await transcriber(uploads).transcribe(
            audio, language_hint=None, duration_seconds=150
        )
    assert uploads.paths() == [("POST", "/upload/v1beta/files")]


async def test_failures_are_classified_by_status_and_never_carry_its_words(
    audio: Path,
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
            await transcriber(google).transcribe(
                audio, language_hint="en", duration_seconds=150
            )
        assert (str(caught.value), caught.value.retryable) == (reason, retryable)
        assert "provider words" not in repr(caught.value)

    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(TranscriptionError, match=f"^{UNAVAILABLE}$"):
        await transcriber(dropped).transcribe(
            audio, language_hint=None, duration_seconds=150
        )  # type: ignore[arg-type]


async def test_a_failed_delete_is_logged_by_type_and_the_call_still_succeeds(
    audio: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(gemini, "INLINE_MAX_BYTES", 8)
    with caplog.at_level(logging.ERROR, logger="dodeal_ai.calls.stt"):
        transcript = await transcriber(Google(delete_status=500)).transcribe(
            audio, language_hint=None, duration_seconds=150
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


async def test_gemini_is_built_with_the_stt_timeout(monkeypatch, audio: Path) -> None:
    """The guard (F-7) on Gemini: the SDK's request carries 600 s."""
    google = Google()
    monkeypatch.setenv("DODEAL_CALL_STT_PROVIDER", "gemini")
    monkeypatch.setenv("DODEAL_CALL_STT_API_KEY", "k1")
    get_settings.cache_clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        built = build_transcribers(get_settings(), http)
        await built["default"].transcribe(
            audio, language_hint=None, duration_seconds=150
        )
    (request,) = google.requests
    assert set(request.extensions["timeout"].values()) == {600}
