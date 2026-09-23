"""FakeTranscriber: a Transcriber that runs no model, for the tests and the demo.

`build_transcriber` never returns it -- there is no switch that selects it. A
test or scripts/call_demo.py constructs it and hands it to the worker itself,
which starts with it only while CALL_DEMO_ALLOW_LOCAL_AUDIO is on.

It checks what a real adapter would need -- the file exists and is not empty
-- and records every call, so a test can assert the worker transcribed once and
with which hint. The words it returns are invented; no real call is in here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dodeal_ai.units.call_intelligence.transcriber import (
    Segment,
    Transcript,
    TranscriptionError,
)

PROVIDER = "fake"
MODEL = "fake-stt-1"

# An invented two-speaker call, mostly English with Arabic turns: mixed.
DEFAULT_SEGMENTS: tuple[Segment, ...] = (
    Segment(
        start_s=0.0,
        end_s=6.5,
        speaker="agent",
        text="Good morning, this is the sales office calling about the viewing.",
        language="en",
        confidence=0.94,
    ),
    Segment(
        start_s=6.5,
        end_s=11.0,
        speaker="lead",
        text="أهلاً، نعم أنا مهتم بالشقة.",
        language="ar",
        confidence=0.88,
    ),
    Segment(
        start_s=11.2,
        end_s=19.0,
        speaker="agent",
        text="Great, shall we book Tuesday at four for the second viewing?",
        language="en",
        confidence=0.91,
    ),
)


@dataclass(frozen=True, slots=True)
class TranscribeCall:
    """One call the fake received: the file's size at the time, and the hint."""

    size_bytes: int
    language_hint: str | None


class FakeTranscriber:
    """Structurally a Transcriber. `segments` is what every call answers with;
    `fail` is raised instead, when set."""

    def __init__(
        self,
        segments: tuple[Segment, ...] = DEFAULT_SEGMENTS,
        *,
        fail: TranscriptionError | None = None,
    ) -> None:
        self.segments = segments
        self.fail = fail
        self.calls: list[TranscribeCall] = []

    async def transcribe(
        self, audio_path: Path, *, language_hint: str | None
    ) -> Transcript:
        size = audio_path.stat().st_size
        self.calls.append(TranscribeCall(size_bytes=size, language_hint=language_hint))
        if size == 0:
            raise TranscriptionError("audio_empty", retryable=False)
        if self.fail is not None:
            raise self.fail
        return Transcript.of(self.segments, provider=PROVIDER, model=MODEL)
