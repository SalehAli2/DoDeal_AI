"""CallsConfig -- a tenant's `unit_b` section, and the only source of a call
threshold, a host list or a switch in this unit (register item 50).

Parsed and stored ONLY. Every switch defaults off, so a tenant that has not
asked for call intelligence has none: no job is admitted, no audio fetched and
no model called. The section is set in the tenant file or at runtime through
the admin route; either way this parser decides what is accepted, and the
override store merges it beside `unit_a` (core/tenant_config.py, item 193).

Two refusals are the point of the section: a callback that is not https (a
transcript in clear on the wire), and calls switched on with no audio host (a
job that could only fetch from anywhere, or from nowhere).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import DEFAULT_ROUTE, route_names
from dodeal_ai.core.tenant_config import ResolvedSection, resolve_section
from dodeal_ai.units.call_intelligence.alarms import words
from dodeal_ai.units.call_intelligence.extras import (
    DEFAULT_WHATSAPP_DIALECT,
    WhatsAppDialect,
)
from dodeal_ai.units.call_intelligence.keywords import MAX_KEYWORD_TERMS
from dodeal_ai.units.call_intelligence.numbers import DEFAULT_COUNTRY_CODE
from dodeal_ai.units.call_intelligence.transcriber import (
    DEFAULT_STT_PROFILE,
    stt_profile_names,
)

__all__ = [
    "UNIT_B_SECTION",
    "CallsConfig",
    "calls_config_of",
    "calls_section_of",
    "new_config_version",
    "parse_unit_b_section",
    "resolve_calls_config",
]

# This unit's section name in a `<tenant>.json` and in the override record.
UNIT_B_SECTION = "unit_b"

# The longest a transcript or a result is held, in seconds: 72 hours. A tenant
# may hold less, never more -- the hold is a data-retention promise.
_MAX_RESULT_TTL_SECONDS = 259_200

# The largest recording fetched, in bytes: 200 MiB. A tenant may lower it and
# never raise it, so a tenant file cannot size our disk.
_MAX_AUDIO_BYTES = 209_715_200

# One DNS name, lower case, no scheme, port, path or wildcard.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")

type _Word = Annotated[str, Field(min_length=1)]
# A canonical name: a project's, not a paragraph.
type _Term = Annotated[str, Field(min_length=1, max_length=120)]


class CallsConfig(BaseModel):
    """One tenant's call rules. Frozen, and every collection is a frozenset,
    so the shared default cannot be changed by a caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Where stage results are POSTed. None: no callback, results by GET only.
    callback_url: str | None = None
    # The hosts a recording may be fetched from, exact names. Empty fetches none.
    audio_hosts: frozenset[str] = frozenset()
    # Below this many seconds a call is done with its outcome label, unpaid.
    min_transcribe_seconds: int = Field(default=30, ge=0)
    # At least this long, and not uncertain, a call is eligible for full analysis.
    scoring_min_seconds: int = Field(default=120, ge=0)
    # How long a job's result is held after it is stored; 72 h is the ceiling.
    result_ttl_seconds: int = Field(
        default=_MAX_RESULT_TTL_SECONDS, gt=0, le=_MAX_RESULT_TTL_SECONDS
    )
    # The largest recording fetched; over it the download stops and is refused.
    max_audio_bytes: int = Field(default=_MAX_AUDIO_BYTES, gt=0, le=_MAX_AUDIO_BYTES)
    # Lead statuses whose calls take the priority queue; stored casefolded.
    priority_statuses: frozenset[_Word] = frozenset({"qualified", "negotiation"})
    # Phrases a later pass may alarm on; stored casefolded. Parsed only.
    alarm_phrases: frozenset[_Word] = frozenset()
    # The company's canonical names for its projects, communities and
    # developers, as written, at most 100. Used after transcription only
    # (keywords.py, extras.py); never sent to the speech-to-text engine.
    keyword_vocabulary: frozenset[_Term] = Field(
        default=frozenset(), max_length=MAX_KEYWORD_TERMS
    )
    # The country code a number said with a leading single 0 is hashed under
    # (numbers.py), 1 to 3 digits: 971, the UAE, where the agencies are. A wrong
    # one hashes every local number as another country's, so none matches.
    phone_country_code: str = Field(
        default=DEFAULT_COUNTRY_CODE, pattern=r"^[1-9][0-9]{0,2}$"
    )
    # How the recording's channels map to speakers (audio.py): "mono" is one
    # mixed track; a stereo setting splits it, the agent on the side named.
    # Stereo on a file that is not two channels fails it, unpaid.
    audio_channels: Literal["mono", "stereo_agent_left", "stereo_agent_right"] = "mono"
    # The model route this tenant's call passes go through (core/llm/routing.py)
    # and the STT profile its calls are transcribed with: "default" is the
    # DODEAL_LLM_* / DODEAL_CALL_STT_* pair. Policy, like every unit_b field.
    model_route: str = DEFAULT_ROUTE
    stt_profile: str = DEFAULT_STT_PROFILE
    # The Arabic dialect a WhatsApp suggestion to an Arabic speaker is written in
    # when the agent's is unknown (extras.py): gulf_ar, the agencies' market. A
    # wrong one reads foreign to the client; a code not of the four is refused.
    whatsapp_default_dialect: WhatsAppDialect = DEFAULT_WHATSAPP_DIALECT

    # The switches, all off: calls_enabled admits jobs at all (403 otherwise);
    # the other five name later passes and are parsed and stored only. A switch
    # left on by mistake is caught by the pass it gates, never here.
    calls_enabled: bool = False
    scoring_enabled: bool = False
    voice_id_enabled: bool = False
    number_detection_enabled: bool = False
    alarm_phrases_enabled: bool = False
    prosody_enabled: bool = False

    # Stamped by the override store on a runtime PUT (core/tenant_config.py);
    # None under a tenant file that sets none, and under the default.
    config_version: str | None = Field(default=None, min_length=1)

    @field_validator("callback_url")
    @classmethod
    def _https_callback(cls, value: str | None) -> str | None:
        """https with a host and no credentials -- http too, for the demo
        only, under CALL_DEMO_ALLOW_LOCAL_AUDIO. Fixed message: never the URL."""
        if value is None:
            return None
        try:
            parts = urlsplit(value)
            has_credentials = parts.username is not None or parts.password is not None
        except ValueError:
            raise ValueError("callback_url") from None
        schemes = (
            {"https", "http"}
            if get_settings().call_demo_allow_local_audio
            else {"https"}
        )
        if parts.scheme not in schemes or not parts.hostname or has_credentials:
            raise ValueError("callback_url")
        return value

    @field_validator("audio_hosts", mode="before")
    @classmethod
    def _hostnames(cls, value: object) -> object:
        """Every entry a bare DNS name, lower-cased here."""
        if not isinstance(value, list | tuple | set | frozenset):
            return value
        hosts = []
        for host in value:
            if not isinstance(host, str) or not _HOSTNAME.fullmatch(host.lower()):
                raise ValueError("audio_hosts")
            hosts.append(host.lower())
        return frozenset(hosts)

    @field_validator("model_route")
    @classmethod
    def _known_route(cls, value: str) -> str:
        """A route this deployment configures; fixed message, never the name."""
        if value not in route_names(get_settings()):
            raise ValueError("model_route")
        return value

    @field_validator("stt_profile")
    @classmethod
    def _known_stt_profile(cls, value: str) -> str:
        """An STT profile this deployment configures; never the name."""
        if value not in stt_profile_names(get_settings()):
            raise ValueError("stt_profile")
        return value

    @field_validator("priority_statuses", "alarm_phrases")
    @classmethod
    def _casefolded(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(word.casefold() for word in value)

    @field_validator("keyword_vocabulary")
    @classmethod
    def _distinct_terms(cls, value: frozenset[str]) -> frozenset[str]:
        """Every term has words, and no two are one name normalised: the
        canonical name a find gives must be unambiguous."""
        split = [tuple(words(term)) for term in value]
        if not all(split) or len(set(split)) != len(split):
            raise ValueError("keyword_vocabulary")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> CallsConfig:
        """Calls on need somewhere to fetch from; full analysis never starts
        below the transcription floor."""
        if self.calls_enabled and not self.audio_hosts:
            raise ValueError("audio_hosts_required")
        if self.scoring_min_seconds < self.min_transcribe_seconds:
            raise ValueError("scoring_min_seconds")
        return self


_DEFAULT_CONFIG = CallsConfig()


def parse_unit_b_section(raw: object) -> CallsConfig:
    """The `unit_b` section as a CallsConfig, or ValueError (pydantic's
    ValidationError is one) for anything it refuses."""
    return CallsConfig.model_validate(raw)


def calls_config_of(resolved: ResolvedSection) -> CallsConfig:
    """A resolved `unit_b` section as this unit's config; the default when the
    resolution found neither an override nor a file."""
    value = resolved.value
    return value if isinstance(value, CallsConfig) else _DEFAULT_CONFIG


async def resolve_calls_config(tenant: str, *, fresh: bool = False) -> CallsConfig:
    """The call rules in force for `tenant`: the runtime override, else the
    file, else the default (everything off). `fresh`: past the process cache,
    RedisError when the store cannot be read."""
    return calls_config_of(await resolve_section(tenant, UNIT_B_SECTION, fresh=fresh))


def new_config_version(body: dict, in_force: object | None) -> str | None:
    """The admin PUT's keeper for this section: the config_version in force,
    whatever the body changes. A unit_b PUT moves only the policy_version: no
    call rule marks a note, and the config_version is what marks are grouped
    by. None -- a first dated one -- only while none is in force."""
    if isinstance(in_force, CallsConfig) and in_force.config_version is not None:
        return in_force.config_version
    return None


def calls_section_of(config: CallsConfig) -> dict[str, object]:
    """A config as the `unit_b` section that produces it, sets as sorted lists:
    what the admin route shows as in force."""
    return {
        name: sorted(value) if isinstance(value, frozenset) else value
        for name, value in config.model_dump().items()
    }
