"""GeminiTranscriber (Unit B): speech-to-text on Google's Gemini API, through
Google's official google-genai SDK, pinned in pyproject.toml.

THE API SURFACE, as Google's transcription guide gives it (read 2026-09-24):
the Interactions API, `client.aio.interactions.create`, with one audio input
and generation_config.transcription_config asking for verbatim text, speaker
diarization and word timestamps, the language hint as language_codes. A file
up to INLINE_MAX_BYTES goes inline, base64; a larger one through the Files
API, deleted in a finally whatever happened. No custom vocabulary is sent:
the tenant's keyword_vocabulary is used after transcription (keywords.py).

ONE REQUEST PER ATTEMPT. The SDK retries on its own on two layers -- the
Interactions client (on 408, 409, 429 and 5xx, with no per-call switch) and
the Files client. Both are switched off in `sdk_client`, so the worker's one
retry (BRD B6) is the only one and no received answer is paid for twice.

THE ANSWER: word_info annotations, each a word with its speaker (spk_N) and
its start and end offsets. Consecutive words of one speaker are one segment,
labelled speaker_N, clamped so segments never overlap. The API names no
language and no confidence: a segment's language is the script most of its
letters are in (ar Arabic, en Latin; with none, the hint, else und), and its
confidence is null.

FAILURES, by HTTP status only, never the provider's message: 408, 429, 5xx, a
timeout or a dropped connection is retryable; any other 4xx is permanent; an
answer that arrived but cannot be read is permanent, never asked again.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.genai._gaos.lib.compat_errors import APIConnectionError
from google.genai._gaos.lib.compat_errors import APIError as InteractionsError
from google.genai._gaos.utils import RetryConfig
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dodeal_ai.units.call_intelligence.evidence import script_letters
from dodeal_ai.units.call_intelligence.transcriber import (
    Segment,
    Transcript,
    TranscriptionError,
)

_logger = logging.getLogger("dodeal_ai.calls.stt")

PROVIDER = "gemini"

# The largest file sent inline: base64 grows it by a third, and the request
# must stay under Gemini's 20 MB inline limit. Larger ones are uploaded.
INLINE_MAX_BYTES = 12 * 1024 * 1024

UNAVAILABLE = "stt_unavailable"
REFUSED = "stt_request_refused"
MALFORMED = "stt_malformed_response"
UNKNOWN_FORMAT = "audio_format_unknown"

# Statuses a later attempt may pass, besides every 5xx.
_RETRYABLE = frozenset({408, 429})

# The formats Google documents for audio input, by their first bytes: (offset,
# signature, MIME type). MPEG frames (mp3, aac) are told apart in audio_mime.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (8, b"WAVE", "audio/wav"),
    (8, b"AIFF", "audio/aiff"),
    (0, b"ID3", "audio/mp3"),
    (0, b"OggS", "audio/ogg"),
    (0, b"fLaC", "audio/flac"),
)
_HEAD_BYTES = 12

_LANGUAGES: dict[str, list[str]] = {"en": ["en"], "ar": ["ar"], "mixed": ["ar", "en"]}
_OFFSET = r"^[0-9]+(\.[0-9]+)?s$"


def audio_mime(head: bytes) -> str | None:
    """The MIME type of a file starting with `head`, or None for a format
    Gemini does not document."""
    for offset, signature, mime in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            return mime
    if len(head) < 2 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return None
    # An MPEG frame: layer bits 00 are ADTS (aac), any other layer is mp3.
    return "audio/aac" if head[1] & 0x06 == 0 else "audio/mp3"


def sdk_client(
    api_key: str,
    http: httpx.AsyncClient,
    *,
    base_url: str | None,
    timeout_seconds: float,
) -> genai.Client:
    """The SDK's client on `http`, with both of its retry layers off: the
    Files client makes one attempt, and the Interactions client -- whose
    public options never go below one retry -- none."""
    client = genai.Client(
        vertexai=False,
        api_key=api_key,
        http_options=types.HttpOptions(
            httpx_async_client=http,
            base_url=base_url,
            timeout=int(timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    client.aio.interactions.sdk_configuration.retry_config = RetryConfig(
        "none", None, False
    )
    return client


def generation_config(language_hint: str | None) -> dict[str, Any]:
    """Verbatim, diarized, word-timed; the hint's languages when it names any."""
    config: dict[str, Any] = {
        "mode": {
            "type": "verbatim",
            "diarization_mode": "speaker",
            "timestamp_granularities": ["word"],
        }
    }
    if language_hint in _LANGUAGES:
        config["language_codes"] = _LANGUAGES[language_hint]
    return {"transcription_config": config}


def failure(error: Exception) -> TranscriptionError:
    """The fixed reason for an SDK failure, by status alone (module docstring)."""
    if isinstance(error, APIConnectionError | httpx.TransportError):
        return TranscriptionError(UNAVAILABLE, retryable=True)
    status = (
        error.status_code
        if isinstance(error, InteractionsError)
        else getattr(error, "code", None)
    )
    if not isinstance(status, int) or status < 400:
        return TranscriptionError(MALFORMED, retryable=False)
    if status in _RETRYABLE or status >= 500:
        return TranscriptionError(UNAVAILABLE, retryable=True)
    return TranscriptionError(REFUSED, retryable=False)


class _Word(BaseModel):
    """One word_info annotation, as this adapter needs it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    text: str
    speaker: str = Field(pattern=r"^spk_[0-9]{1,3}$")
    start_offset: str = Field(pattern=_OFFSET)
    end_offset: str = Field(pattern=_OFFSET)

    @property
    def start(self) -> float:
        return float(self.start_offset[:-1])

    @property
    def end(self) -> float:
        return float(self.end_offset[:-1])


def _words(answer: dict[str, Any]) -> list[_Word]:
    """Every word of the answer's text output; ValidationError for a word
    missing its speaker or its times, MALFORMED for text with no words."""
    parts = [
        part
        for step in answer.get("steps") or ()
        if step.get("type") == "model_output"
        for part in step.get("content") or ()
        if part.get("type") == "text"
    ]
    found = [
        _Word.model_validate(note)
        for part in parts
        for note in part.get("annotations") or ()
        if note.get("type") == "word_info"
    ]
    if not found and any(str(part.get("text") or "").strip() for part in parts):
        raise TranscriptionError(MALFORMED, retryable=False)
    return found


def _language(text: str, hint: str | None) -> str:
    letters = script_letters(text)
    if letters["ar"] or letters["en"]:
        return "ar" if letters["ar"] >= letters["en"] else "en"
    return hint if hint in ("ar", "en") else "und"


def segments_of(words: list[_Word], hint: str | None) -> tuple[Segment, ...]:
    """Consecutive words of one speaker as one segment, in time order, each
    starting no earlier than the one before it ended."""
    turns: list[list[_Word]] = []
    for word in sorted(words, key=lambda word: word.start):
        if turns and turns[-1][-1].speaker == word.speaker:
            turns[-1].append(word)
        else:
            turns.append([word])
    segments: list[Segment] = []
    floor = 0.0
    for turn in turns:
        start = max(turn[0].start, floor)
        floor = max(start, *(word.end for word in turn))
        text = " ".join(word.text for word in turn)
        segments.append(
            Segment(
                start_s=start,
                end_s=floor,
                speaker=f"speaker_{turn[0].speaker.removeprefix('spk_')}",
                text=text,
                language=_language(text, hint),
                confidence=None,
            )
        )
    return tuple(segments)


def _head(path: Path) -> bytes:
    with path.open("rb") as audio:
        return audio.read(_HEAD_BYTES)


class GeminiTranscriber:
    """The Transcriber on Gemini (module docstring). `http` is the worker's
    pool for speech-to-text, which the worker closes."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        http: httpx.AsyncClient,
        base_url: str | None,
        timeout_seconds: float,
    ) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._client = sdk_client(
            api_key, http, base_url=base_url, timeout_seconds=timeout_seconds
        )

    async def transcribe(
        self, audio_path: Path, *, language_hint: str | None
    ) -> Transcript:
        mime = audio_mime(await asyncio.to_thread(_head, audio_path))
        if mime is None:
            raise TranscriptionError(UNKNOWN_FORMAT, retryable=False)
        try:
            async with self._audio(audio_path, mime) as audio:
                answer: Any = await self._client.aio.interactions.create(
                    model=self._model,
                    input=[audio],
                    generation_config=generation_config(language_hint),
                    timeout=self._timeout_seconds,
                )
        except (InteractionsError, genai_errors.APIError, httpx.TransportError) as e:
            raise failure(e) from None
        # The SDK's Interaction, or a plain dict for a body its types do not
        # fit; never a stream, since none was asked for.
        received = (
            answer.model_dump(mode="json") if isinstance(answer, BaseModel) else answer
        )
        try:
            words = _words(received)
            return Transcript.of(
                segments_of(words, language_hint), provider=PROVIDER, model=self._model
            )
        except ValidationError:
            raise TranscriptionError(MALFORMED, retryable=False) from None

    @asynccontextmanager
    async def _audio(self, path: Path, mime: str) -> AsyncIterator[Any]:
        """The audio input: inline when small, else an upload deleted after."""
        size = (await asyncio.to_thread(path.stat)).st_size
        if size <= INLINE_MAX_BYTES:
            data = await asyncio.to_thread(path.read_bytes)
            yield {
                "type": "audio",
                "data": base64.b64encode(data).decode("ascii"),
                "mime_type": mime,
            }
            return
        uploaded = await self._client.aio.files.upload(
            file=str(path), config={"mime_type": mime}
        )
        try:
            yield {"type": "audio", "uri": uploaded.uri, "mime_type": mime}
        finally:
            await self._delete(uploaded.name)

    async def _delete(self, name: str | None) -> None:
        """Delete an upload; a failure is logged by type and never fails the
        call (Google deletes uploads after 48 hours anyway)."""
        try:
            await self._client.aio.files.delete(name=str(name))
        except (genai_errors.APIError, httpx.TransportError) as error:
            _logger.error(
                "stt_upload_delete_failed",
                extra={
                    "reason_code": "stt_upload_delete_failed",
                    "provider": PROVIDER,
                    "error_type": type(error).__name__,
                },
            )
