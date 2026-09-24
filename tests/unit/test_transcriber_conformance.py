"""The Transcriber seam and its conformance suite (Unit B). Every adapter is run
through the same assertions -- ordered, non-overlapping segments with speakers,
and a profile and an uncertainty flag its own segments agree with. The fake
(demo only), and each real adapter on a recorded response, never a live call."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.units.call_intelligence.fake_transcriber import (
    DEFAULT_SEGMENTS,
    FakeTranscriber,
)
from dodeal_ai.units.call_intelligence.gemini import GeminiTranscriber
from dodeal_ai.units.call_intelligence.http_stt import (
    DiarizedHttpTranscriber,
    OpenAiCompatibleTranscriber,
)
from dodeal_ai.units.call_intelligence.stt import build_transcribers
from dodeal_ai.units.call_intelligence.transcriber import (
    LanguageProfile,
    Segment,
    Transcriber,
    Transcript,
    TranscriptionError,
    is_uncertain,
    profile_of,
)

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "stt"


def _gemini() -> Transcriber:
    """Gemini through the real SDK, answered with a recorded response."""
    answer = json.loads(
        (RECORDED / "gemini_interaction.json").read_text(encoding="utf-8")
    )
    return GeminiTranscriber(
        model="gemini-3.5-transcribe",
        api_key="test-key",
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=answer))
        ),
        base_url=None,
        timeout_seconds=30,
    )


def _http(kind: type, fixture: str) -> Callable[[], Transcriber]:
    """An HTTP adapter answered with its recorded response."""
    answer = json.loads((RECORDED / fixture).read_text(encoding="utf-8"))

    def build() -> Transcriber:
        transcriber: Transcriber = kind(
            base_url="http://stt.test",
            model="m1",
            api_key="test-key",
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json=answer)
                )
            ),
            timeout_seconds=30,
        )
        return transcriber

    return build


# Every Transcriber this repo has; a real adapter is added here when it lands.
IMPLEMENTATIONS: dict[str, Callable[[], Transcriber]] = {
    "fake": FakeTranscriber,
    "gemini": _gemini,
    "openai_compatible": _http(OpenAiCompatibleTranscriber, "openai_verbose.json"),
    "diarized_http": _http(DiarizedHttpTranscriber, "diarized_http.json"),
}


def _segment(
    start: float, end: float, *, language: str = "en", confidence: float = 0.9
) -> Segment:
    return Segment(
        start_s=start,
        end_s=end,
        speaker="agent",
        text="invented words",
        language=language,
        confidence=confidence,
    )


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "call.audio"
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 120)
    return path


# --- the conformance suite ------------------------------------------------------


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def transcriber(request: pytest.FixtureRequest) -> Transcriber:
    return IMPLEMENTATIONS[request.param]()


def test_each_implementation_is_a_transcriber(transcriber: Transcriber) -> None:
    assert isinstance(transcriber, Transcriber)


@pytest.mark.parametrize("hint", [None, "en", "ar", "mixed"])
async def test_segments_are_ordered_and_never_overlap(
    transcriber: Transcriber, audio: Path, hint: str | None
) -> None:
    transcript = await transcriber.transcribe(
        audio, language_hint=hint, duration_seconds=150
    )
    for segment in transcript.segments:
        assert 0 <= segment.start_s <= segment.end_s
    for before, after in zip(
        transcript.segments, transcript.segments[1:], strict=False
    ):
        assert before.end_s <= after.start_s


async def test_every_segment_names_a_speaker(
    transcriber: Transcriber, audio: Path
) -> None:
    transcript = await transcriber.transcribe(
        audio, language_hint=None, duration_seconds=150
    )
    assert transcript.segments
    assert all(segment.speaker for segment in transcript.segments)
    assert set(transcript.speakers) == {s.speaker for s in transcript.segments}


async def test_the_profile_and_the_flag_agree_with_the_segments(
    transcriber: Transcriber, audio: Path
) -> None:
    transcript = await transcriber.transcribe(
        audio, language_hint="en", duration_seconds=150
    )
    assert transcript.language_profile is profile_of(transcript.segments)
    # An adapter may add a fixed doubt its segments cannot show (no speakers).
    doubted = is_uncertain(transcript.segments) or bool(transcript.uncertain_reasons)
    assert transcript.uncertain is doubted
    assert transcript.provider and transcript.model


async def test_segment_text_is_never_on_a_repr(
    transcriber: Transcriber, audio: Path
) -> None:
    transcript = await transcriber.transcribe(
        audio, language_hint=None, duration_seconds=150
    )
    shown = repr(transcript) + "".join(repr(s) for s in transcript.segments)
    assert all(segment.text not in shown for segment in transcript.segments)


# --- the rules the suite holds every adapter to -------------------------------


@pytest.mark.parametrize(
    ("languages", "profile"),
    [
        (("en", "en", "en", "en", "ar"), LanguageProfile.MOSTLY_EN),
        (("ar", "ar", "ar", "ar", "en"), LanguageProfile.MOSTLY_AR),
        (("en", "ar", "en", "ar", "en"), LanguageProfile.MIXED),
        (("fr", "fr", "en", "ar", "fr"), LanguageProfile.OTHER),
    ],
)
def test_the_profile_is_each_languages_share_of_the_time(
    languages: tuple[str, ...], profile: LanguageProfile
) -> None:
    segments = tuple(
        _segment(n, n + 1, language=code) for n, code in enumerate(languages)
    )
    assert profile_of(segments) is profile


def test_no_speech_is_uncertain_and_other() -> None:
    silent = Transcript.of((), provider="p", model="m")
    assert (silent.uncertain, silent.language_profile) == (True, LanguageProfile.OTHER)
    instant = (_segment(1, 1),)
    assert is_uncertain(instant) and profile_of(instant) is LanguageProfile.OTHER


def test_low_confidence_is_uncertain_by_spoken_time() -> None:
    """One long doubtful stretch outweighs a short confident one."""
    doubtful = (_segment(0, 1, confidence=0.99), _segment(1, 10, confidence=0.3))
    assert is_uncertain(doubtful)
    assert not is_uncertain(
        (_segment(0, 9, confidence=0.9), _segment(9, 10, confidence=0.1))
    )


def test_a_transcript_that_contradicts_its_segments_is_refused() -> None:
    segments = (_segment(0, 5),)
    good = Transcript.of(segments, provider="p", model="m")
    for change in (
        {"uncertain": True},
        {"language_profile": LanguageProfile.MOSTLY_AR},
        {"segments": (_segment(0, 5), _segment(4, 6))},
    ):
        with pytest.raises(ValidationError):
            Transcript(**{**good.model_dump(), **change})


@pytest.mark.parametrize(
    "fields",
    [
        {"start_s": 5.0, "end_s": 4.0},
        {"language": "English"},
        {"confidence": 1.5},
        {"speaker": ""},
    ],
)
def test_a_malformed_segment_is_refused(fields: dict) -> None:
    base = _segment(0, 1).model_dump()
    with pytest.raises(ValidationError):
        Segment(**{**base, **fields})


# --- the fake and the factory ----------------------------------------------------


async def test_the_fake_records_each_call_and_its_hint(audio: Path) -> None:
    fake = FakeTranscriber()
    transcript = await fake.transcribe(audio, language_hint="ar", duration_seconds=150)
    assert transcript.segments == DEFAULT_SEGMENTS
    assert transcript.language_profile is LanguageProfile.MIXED
    assert transcript.spoken_seconds == pytest.approx(18.8)
    assert transcript.speakers == ("agent", "lead")
    assert [(c.size_bytes, c.language_hint) for c in fake.calls] == [(132, "ar")]


async def test_the_fake_refuses_empty_audio_and_raises_when_told(
    tmp_path: Path, audio: Path
) -> None:
    empty = tmp_path / "empty.audio"
    empty.write_bytes(b"")
    with pytest.raises(TranscriptionError, match="^audio_empty$"):
        await FakeTranscriber().transcribe(
            empty, language_hint=None, duration_seconds=150
        )
    failing = FakeTranscriber(fail=TranscriptionError("provider_down", retryable=True))
    with pytest.raises(TranscriptionError) as caught:
        await failing.transcribe(audio, language_hint=None, duration_seconds=150)
    assert caught.value.retryable


async def test_the_factory_refuses_with_no_adapter_and_never_builds_the_fake() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConfigError, match="^stt_not_configured$"):
            build_transcribers(get_settings(), http)
