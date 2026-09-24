"""The Transcriber seam (Unit B): a recording on disk in, a Transcript out.

ONE METHOD, like the LLM seam: `transcribe(audio_path, *, language_hint,
duration_seconds)`. A provider adapter is a class that satisfies it; nothing
else in the unit knows which provider ran. The adapters are built per STT
profile by stt.py, which never builds the fake: a test or the demo hands it in
directly (fake_transcriber.py), refused unless CALL_DEMO_ALLOW_LOCAL_AUDIO is
on.

THE TRANSCRIPT CHECKS ITSELF. Segments are in order and never overlap, every
segment names a speaker, and the two judgements made of the whole -- the
language profile and the uncertainty flag -- are computed by THIS module from
the segments (`Transcript.of`) and re-checked on construction. An adapter
cannot hand back a profile or a flag its own segments contradict, so every
provider is held to one rule. The conformance suite runs against each one.

Code may doubt a transcript its segments do not: poor audio (audio.py) or
speakers whose roles are unclear. Each such doubt is a fixed reason code in
`uncertain_reasons`, and any reason makes the transcript uncertain.

Segment text is the call's content: never on a repr, never logged.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dodeal_ai.core.config import ConfigError, Settings

# A language's share of the spoken time at which the call is "mostly" it, and
# English and Arabic together at which it is "mixed". Below: "other".
MOSTLY_SHARE = 0.8

# Below this time-weighted mean confidence, or with no speech at all, the
# transcript is uncertain: it is stored and delivered, never fully analysed.
MIN_MEAN_CONFIDENCE = 0.6

# Why a transcript with no segments at all is uncertain: nothing was said
# that an engine heard, so there is nothing for any pass to read.
NO_SPEECH = "no_speech"

# The STT profile built from DODEAL_CALL_STT_*, which every tenant is on until
# its unit_b stt_profile names one of CALL_STT_PROFILES.
DEFAULT_STT_PROFILE = "default"


def stt_profile_names(settings: Settings) -> frozenset[str]:
    """Every STT profile a tenant may name: "default" and each configured one."""
    return frozenset({DEFAULT_STT_PROFILE, *settings.call_stt_profiles})


class LanguageProfile(StrEnum):
    MOSTLY_EN = "mostly_en"
    MOSTLY_AR = "mostly_ar"
    MIXED = "mixed"
    OTHER = "other"


class TranscriptionError(Exception):
    """A provider failure. str() is the fixed reason code; `retryable` says
    whether the same audio could succeed later. Never the provider's message."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


class TranscriberNotConfigured(ConfigError):
    """No speech-to-text adapter exists for this deployment."""

    def __init__(self) -> None:
        super().__init__("stt_not_configured")


class HandedTranscriberRefused(ConfigError):
    """A transcriber was handed to a worker with the demo flag off."""

    def __init__(self) -> None:
        super().__init__("fake_transcriber_needs_demo")


class Segment(BaseModel):
    """One stretch of one speaker's speech, in seconds from the start."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    speaker: str = Field(min_length=1, max_length=64)
    text: str = Field(repr=False)
    # An ISO 639 code, lower case: en, ar, or whatever else was spoken.
    language: str = Field(pattern=r"^[a-z]{2,3}$")
    # None when the engine reports no confidence (Gemini): never invented.
    confidence: float | None = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _forwards(self) -> Segment:
        if self.end_s < self.start_s:
            raise ValueError("segment_backwards")
        return self

    @property
    def seconds(self) -> float:
        return self.end_s - self.start_s


def profile_of(segments: tuple[Segment, ...]) -> LanguageProfile:
    """The language profile, by each language's share of the spoken time."""
    total = sum(segment.seconds for segment in segments)
    if total <= 0:
        return LanguageProfile.OTHER
    share = {
        code: sum(s.seconds for s in segments if s.language == code) / total
        for code in ("en", "ar")
    }
    if share["en"] >= MOSTLY_SHARE:
        return LanguageProfile.MOSTLY_EN
    if share["ar"] >= MOSTLY_SHARE:
        return LanguageProfile.MOSTLY_AR
    if share["en"] + share["ar"] >= MOSTLY_SHARE:
        return LanguageProfile.MIXED
    return LanguageProfile.OTHER


def is_uncertain(segments: tuple[Segment, ...]) -> bool:
    """No speech, or a time-weighted mean confidence below the floor, over the
    segments that carry one; with none carrying one, only no speech."""
    if sum(segment.seconds for segment in segments) <= 0:
        return True
    rated = [(s.confidence, s.seconds) for s in segments if s.confidence is not None]
    total = sum(seconds for _, seconds in rated)
    if total <= 0:
        return False
    return sum(c * seconds for c, seconds in rated) / total < MIN_MEAN_CONFIDENCE


class Transcript(BaseModel):
    """A whole call's transcript. Build it with `Transcript.of`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segments: tuple[Segment, ...] = Field(repr=False)
    language_profile: LanguageProfile
    uncertain: bool
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    # Why code doubts it beyond its segments' confidence, fixed codes only.
    uncertain_reasons: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        segments: tuple[Segment, ...],
        *,
        provider: str,
        model: str,
        reasons: tuple[str, ...] = (),
    ) -> Transcript:
        """The transcript of `segments`, profile and flag computed here."""
        return cls(
            segments=segments,
            language_profile=profile_of(segments),
            uncertain=is_uncertain(segments) or bool(reasons),
            provider=provider,
            model=model,
            uncertain_reasons=reasons,
        )

    def doubted(self, *reasons: str) -> Transcript:
        """This transcript with `reasons` added: uncertain once there is one."""
        added = tuple(dict.fromkeys((*self.uncertain_reasons, *reasons)))
        return Transcript.of(
            self.segments, provider=self.provider, model=self.model, reasons=added
        )

    @model_validator(mode="after")
    def _consistent(self) -> Transcript:
        """In order, never overlapping, and judged by this module's rules."""
        for before, after in zip(self.segments, self.segments[1:], strict=False):
            if after.start_s < before.end_s:
                raise ValueError("segments_overlap")
        if self.language_profile is not profile_of(self.segments):
            raise ValueError("language_profile")
        doubted = is_uncertain(self.segments) or bool(self.uncertain_reasons)
        if self.uncertain is not doubted:
            raise ValueError("uncertain")
        return self

    @property
    def speakers(self) -> tuple[str, ...]:
        """Every speaker label, in order of first speech."""
        return tuple(dict.fromkeys(segment.speaker for segment in self.segments))

    @property
    def spoken_seconds(self) -> float:
        return sum(segment.seconds for segment in self.segments)


@runtime_checkable
class Transcriber(Protocol):
    """Exactly one method. `language_hint` is the CRM's guess (en, ar, mixed)
    or None; an adapter may use it and must not trust it. `duration_seconds`
    is the call's length as the worker knows it, for an engine whose limits
    depend on it (Gemini's diarization)."""

    async def transcribe(
        self, audio_path: Path, *, language_hint: str | None, duration_seconds: float
    ) -> Transcript:
        """Transcribe the file at `audio_path`, which exists for the call only.
        Raises TranscriptionError for anything the provider could not do."""
