"""The two HTTP speech-to-text adapters (Unit B), for a company that runs its
own engine or buys one behind an OpenAI-style endpoint.

  OpenAiCompatibleTranscriber  POST {base}/audio/transcriptions, multipart,
      response_format verbose_json with segment timestamps (OpenAI's shape,
      served by Whisper-style servers). It names no speakers: every segment
      is "unknown", which no roles pass can map, so a mono call is uncertain
      (stt_no_speakers); a stereo call's sides are named by their channel.
  DiarizedHttpTranscriber  the owner's audio service, to the contract in
      docs/contracts/audio_service.md: segments with speakers, each label
      renamed speaker_N in order of first speech for the roles pass.

The file is always 16 kHz mono FLAC, converted by the worker (audio.py), and
goes as audio/flac.

ONE REQUEST PER ATTEMPT, never retried here: the worker's one retry (BRD B6)
is the only one. The key goes as a bearer token and is never logged.
FAILURES, by HTTP status only, never the body: 408, 429, 5xx, a timeout or a
dropped connection is retryable; any other 4xx is permanent; a 200 that
cannot be read is permanent, never asked again.

Segments come back in time order, each starting no earlier than the one before
it ended. A language the engine names per segment is kept when it is an ISO
639 code; otherwise the script decides (evidence.script_language). Confidence
is the engine's own, 0 to 1, or null: an avg_logprob is not a confidence.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dodeal_ai.units.call_intelligence.audio import ENGINE_MIME
from dodeal_ai.units.call_intelligence.evidence import script_language
from dodeal_ai.units.call_intelligence.gemini import MALFORMED, REFUSED, UNAVAILABLE
from dodeal_ai.units.call_intelligence.transcriber import (
    Segment,
    Transcript,
    TranscriptionError,
)

OPENAI_COMPATIBLE = "openai_compatible"
DIARIZED_HTTP = "diarized_http"

# The label of a segment whose engine names no speaker, and why its
# transcript is uncertain.
NO_SPEAKER = "unknown"
NO_SPEAKERS = "stt_no_speakers"

_RETRYABLE = frozenset({408, 429})
_ISO_639 = re.compile(r"^[a-z]{2,3}$")
# The hint as an ISO 639 code; "mixed" is left for the engine to find.
_HINTS = {"en": "en", "ar": "ar"}


class _Said(BaseModel):
    """One segment as either engine returns it."""

    model_config = ConfigDict(extra="ignore")

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker: str | None = Field(default=None, min_length=1, max_length=64)
    language: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    segments: list[_Said]


def status_failure(status: int) -> TranscriptionError:
    """The fixed reason for an HTTP status that is not 200."""
    if status in _RETRYABLE or status >= 500:
        return TranscriptionError(UNAVAILABLE, retryable=True)
    if 400 <= status < 500:
        return TranscriptionError(REFUSED, retryable=False)
    return TranscriptionError(MALFORMED, retryable=False)


def ordered(
    said: Sequence[_Said], labels: dict[str, str], hint: str | None
) -> tuple[Segment, ...]:
    """The engine's segments in time order, clamped so none overlaps the one
    before it, each speaker renamed by `labels` (unknown for none)."""
    segments: list[Segment] = []
    floor = 0.0
    for part in sorted(said, key=lambda part: part.start):
        start = max(part.start, floor)
        floor = max(part.end, start)
        language = (part.language or "").lower().split("-")[0]
        segments.append(
            Segment(
                start_s=start,
                end_s=floor,
                speaker=labels.get(part.speaker or "", NO_SPEAKER),
                text=part.text.strip(),
                language=language
                if _ISO_639.match(language)
                else script_language(part.text, hint),
                confidence=part.confidence,
            )
        )
    return tuple(segments)


class _HttpTranscriber(ABC):
    """What both adapters share: one multipart POST, classified by status."""

    provider: str
    path: str

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        http: httpx.AsyncClient,
        timeout_seconds: float,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}{self.path}"
        self._model = model
        self._api_key = api_key
        self._http = http
        self._timeout_seconds = timeout_seconds

    @abstractmethod
    def _fields(self, language_hint: str | None) -> dict[str, Any]:
        """The form fields beside the file."""

    @abstractmethod
    def _segments(self, answer: _Answer, hint: str | None) -> Transcript:
        """The transcript of a readable answer."""

    async def transcribe(
        self, audio_path: Path, *, language_hint: str | None
    ) -> Transcript:
        data = await asyncio.to_thread(audio_path.read_bytes)
        try:
            response = await self._http.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=self._fields(language_hint),
                files={"file": (audio_path.name, data, ENGINE_MIME)},
                timeout=self._timeout_seconds,
            )
        except httpx.TransportError:
            raise TranscriptionError(UNAVAILABLE, retryable=True) from None
        if response.status_code != 200:
            raise status_failure(response.status_code)
        try:
            answer = _Answer.model_validate_json(response.content)
            return self._segments(answer, language_hint)
        except ValidationError:
            raise TranscriptionError(MALFORMED, retryable=False) from None


class OpenAiCompatibleTranscriber(_HttpTranscriber):
    """POST {base}/audio/transcriptions, verbose_json with segment times."""

    provider = OPENAI_COMPATIBLE
    path = "/audio/transcriptions"

    def _fields(self, language_hint: str | None) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "model": self._model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        }
        if language_hint in _HINTS:
            fields["language"] = _HINTS[language_hint]
        return fields

    def _segments(self, answer: _Answer, hint: str | None) -> Transcript:
        segments = ordered(answer.segments, {}, hint)
        return Transcript.of(
            segments,
            provider=self.provider,
            model=self._model,
            reasons=(NO_SPEAKERS,),
        )


class DiarizedHttpTranscriber(_HttpTranscriber):
    """The owner's audio service (docs/contracts/audio_service.md)."""

    provider = DIARIZED_HTTP
    path = "/v1/transcriptions"

    def _fields(self, language_hint: str | None) -> dict[str, Any]:
        fields: dict[str, Any] = {"model": self._model, "diarize": "true"}
        if language_hint is not None:
            fields["language_hint"] = language_hint
        return fields

    def _segments(self, answer: _Answer, hint: str | None) -> Transcript:
        heard = dict.fromkeys(
            part.speaker
            for part in sorted(answer.segments, key=lambda part: part.start)
            if part.speaker is not None
        )
        labels = {label: f"speaker_{n}" for n, label in enumerate(heard, start=1)}
        segments = ordered(answer.segments, labels, hint)
        reasons = () if heard else (NO_SPEAKERS,)
        return Transcript.of(
            segments, provider=self.provider, model=self._model, reasons=reasons
        )
