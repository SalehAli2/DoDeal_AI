"""What the CRM sends to push a call job, and what it is told back (register
item 50). The body is `extra="forbid"`: a field this schema does not name --
a callback URL, a transcript, a note -- is a 422, never quietly ignored.

The audio link and the two hashes and the voiceprint are carried, validated
and stored; none of them is ever logged or echoed back (core/errors.py drops a
refused value from the 422 as well).
"""

from __future__ import annotations

import base64
import binascii
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.jobs import JobStatus

# The longest audio link accepted, in characters. A signed link with its
# query string is a few hundred; far past this is not a link we should fetch.
MAX_AUDIO_URL_CHARS = 4096

# The longest voiceprint accepted, in base64 characters (12 KiB decoded). An
# embedding is a few KiB; the cap keeps a push well under the body limit.
MAX_VOICEPRINT_CHARS = 16_384

# A phone number's SHA-256, as the CRM hashes it: 64 lower-case hex.
_PHONE_HASH = r"^[0-9a-f]{64}$"


def _schemes() -> frozenset[str]:
    """https, and http as well only under the demo's local-audio flag."""
    if get_settings().call_demo_allow_local_audio:
        return frozenset({"https", "http"})
    return frozenset({"https"})


class CallJobRequest(BaseModel):
    """One recorded call to transcribe. Ids are the CRM's, all positive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: int = Field(ge=1)
    lead_id: int = Field(ge=1)
    author_id: int = Field(ge=1)
    duration_seconds: int = Field(ge=0)
    recorded_at: AwareDatetime
    audio_url: str = Field(min_length=1, max_length=MAX_AUDIO_URL_CHARS)
    audio_url_expires_at: AwareDatetime
    language_hint: Literal["en", "ar", "mixed"] | None = None
    lead_status: str | None = Field(default=None, min_length=1, max_length=64)
    call_outcome: Literal["answered", "voicemail", "no_answer"] | None = None
    lead_phone_hash: str | None = Field(default=None, pattern=_PHONE_HASH)
    agent_phone_hash: str | None = Field(default=None, pattern=_PHONE_HASH)
    agent_voiceprint: str | None = Field(
        default=None, min_length=1, max_length=MAX_VOICEPRINT_CHARS
    )

    @field_validator("audio_url")
    @classmethod
    def _https_link(cls, value: str) -> str:
        """https with a host and no credentials -- http too under the demo's
        CALL_DEMO_ALLOW_LOCAL_AUDIO. Which hosts are allowed is the download's
        rule (core/audio_download.py), not the schema's."""
        try:
            parts = urlsplit(value)
            has_credentials = parts.username is not None or parts.password is not None
        except ValueError:
            raise ValueError("audio_url") from None
        if parts.scheme not in _schemes() or not parts.hostname or has_credentials:
            raise ValueError("audio_url")
        return value

    @field_validator("agent_voiceprint")
    @classmethod
    def _base64(cls, value: str | None) -> str | None:
        """Strict base64, so a voiceprint is bytes and never free text."""
        if value is None:
            return None
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("agent_voiceprint") from None
        return value


class CallJobAccepted(BaseModel):
    """202: the call's job, new or the one it already had, and where it is."""

    job_id: str
    status: JobStatus
