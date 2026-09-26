"""The language a call's summary and CRM note are written in, decided in code
and never by a model -- from what the roles pass heard each side speak when it
answered (roles.py), else from the transcript's script and time.

THE SPOKEN LANGUAGES are the roles pass's call_languages: one code per side,
the client's and the agent's, each quoted from that side's own segment. They
are used only when the roles pass answered and its mapping was applied; a
side it could not hear is None.

  every side heard in Arabic         mostly_ar
  every side heard in English        mostly_en
  one side Arabic, the other English mixed
  anything else (Urdu, French, ...)  other

THE FALLBACK, when no side was heard (the pass did not run, failed, or its
mapping was not applied): the transcript's own profile (transcriber.py), by
each segment's language share of the spoken time.

THE SUMMARY LANGUAGE from the profile:

  mostly_ar  ar
  mostly_en  en
  mixed      whichever of Arabic and English has more words, counted over the
             segments spoken in it; a tie is ar
  other      en

Only a mixed call looks further, at words, because time favours whoever speaks
slowly. A word is a whitespace-separated token of a segment's text.

COACHED LANGUAGES (BRD B1): a call is coached and scored only when its
client's language is on the tenant's coaching_languages, until each language
is tested; otherwise both are null with language_not_enabled (wave2.py). The
client's language as heard decides; unheard, the script fallback's profile:
mostly_en needs en listed, mostly_ar an Arabic code, mixed both, other never.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from dodeal_ai.units.call_intelligence.transcriber import (
    LanguageProfile,
    Segment,
    Transcript,
    profile_of,
)

type SummaryLanguage = Literal["ar", "en"]

# The codes a side's language is answered in: the six Arabic dialects, then
# the other languages the product covers, then other for any language else.
type CallLanguage = Literal[
    "gulf_ar",
    "egyptian_ar",
    "levantine_ar",
    "iraqi_ar",
    "maghrebi_ar",
    "msa_ar",
    "en",
    "hi",
    "ur",
    "ru",
    "zh",
    "fr",
    "fa",
    "tr",
    "other",
]
CALL_LANGUAGES: tuple[str, ...] = get_args(CallLanguage.__value__)
ARABIC_LANGUAGES = frozenset(code for code in CALL_LANGUAGES if code.endswith("_ar"))
ENGLISH = "en"
OTHER = "other"

# The twelve languages the product covers: a message may be asked for in any.
type ProductLanguage = Literal[
    "gulf_ar",
    "egyptian_ar",
    "levantine_ar",
    "iraqi_ar",
    "en",
    "hi",
    "ur",
    "ru",
    "zh",
    "fr",
    "fa",
    "tr",
]

PRODUCT_LANGUAGES: tuple[str, ...] = get_args(ProductLanguage.__value__)

# The codes a tenant may coach and score calls in: every one but other.
type CoachedLanguage = Literal[
    "gulf_ar",
    "egyptian_ar",
    "levantine_ar",
    "iraqi_ar",
    "maghrebi_ar",
    "msa_ar",
    "en",
    "hi",
    "ur",
    "ru",
    "zh",
    "fr",
    "fa",
    "tr",
]
# The languages tested so far: the four Arabic dialects of the product and
# English.
DEFAULT_COACHING_LANGUAGES: frozenset[CoachedLanguage] = frozenset(
    {"gulf_ar", "egyptian_ar", "levantine_ar", "iraqi_ar", "en"}
)
# Why a call gets no coaching and no score (BRD B1).
LANGUAGE_NOT_ENABLED = "language_not_enabled"

# Where the spoken languages came from, for the stage-1 languages block.
FROM_MODEL = "model"
FROM_SCRIPT = "script"


@dataclass(frozen=True, slots=True)
class Spoken:
    """Each side's language as the roles pass heard it; None when unheard."""

    client: str | None = None
    agent: str | None = None

    @property
    def heard(self) -> bool:
        return self.client is not None or self.agent is not None


UNHEARD = Spoken()


def _family(code: str) -> str:
    """ar for an Arabic dialect, en for English, other for anything else."""
    if code in ARABIC_LANGUAGES:
        return "ar"
    return ENGLISH if code == ENGLISH else OTHER


def call_profile(transcript: Transcript, spoken: Spoken = UNHEARD) -> LanguageProfile:
    """The call's language profile: from the spoken languages when a side was
    heard, else the transcript's own (the module docstring's tables)."""
    if not spoken.heard:
        return transcript.language_profile
    families = {_family(code) for code in (spoken.client, spoken.agent) if code}
    if families == {"ar"}:
        return LanguageProfile.MOSTLY_AR
    if families == {ENGLISH}:
        return LanguageProfile.MOSTLY_EN
    if families == {"ar", ENGLISH}:
        return LanguageProfile.MIXED
    return LanguageProfile.OTHER


def summary_language(
    transcript: Transcript, spoken: Spoken = UNHEARD
) -> SummaryLanguage:
    """ar or en for this call, by the tables in the module docstring."""
    profile = call_profile(transcript, spoken)
    if profile is LanguageProfile.MOSTLY_AR:
        return "ar"
    if profile is not LanguageProfile.MIXED:
        return "en"
    words = {
        code: sum(
            len(segment.text.split())
            for segment in transcript.segments
            if segment.language == code
        )
        for code in ("ar", "en")
    }
    return "en" if words["en"] > words["ar"] else "ar"


def message_language(code: str | None, fallback: SummaryLanguage) -> str:
    """The language a message to a side who speaks `code` is written in: ar
    for any Arabic dialect, the code itself for another language the product
    covers, and `fallback` for other or unheard."""
    if code is None or code == OTHER:
        return fallback
    return "ar" if code in ARABIC_LANGUAGES else code


def coached(
    spoken: Spoken, segments: tuple[Segment, ...], enabled: frozenset[str]
) -> bool:
    """Whether the call is coached and scored (the module docstring's rule)."""
    if spoken.client is not None:
        return spoken.client in enabled
    profile = profile_of(segments)
    arabic = bool(enabled & ARABIC_LANGUAGES)
    if profile is LanguageProfile.MOSTLY_AR:
        return arabic
    if profile is LanguageProfile.MOSTLY_EN:
        return ENGLISH in enabled
    return profile is LanguageProfile.MIXED and arabic and ENGLISH in enabled


def languages_block(transcript: Transcript, spoken: Spoken) -> dict[str, object]:
    """Stage 1's languages block: each side's code, the profile, and whether
    they came from the model or the script fallback."""
    return {
        "client": spoken.client,
        "agent": spoken.agent,
        "profile": call_profile(transcript, spoken).value,
        "source": FROM_MODEL if spoken.heard else FROM_SCRIPT,
    }


def spoken_of(block: object) -> Spoken:
    """The spoken languages back from a stored languages block; unheard for
    a block that is missing, from the fallback, or holds an unknown code."""
    if not isinstance(block, dict) or block.get("source") != FROM_MODEL:
        return UNHEARD
    sides = [block.get("client"), block.get("agent")]
    if not all(side is None or side in CALL_LANGUAGES for side in sides):
        return UNHEARD
    client, agent = sides
    return Spoken(
        client=client if isinstance(client, str) else None,
        agent=agent if isinstance(agent, str) else None,
    )
