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

ONE SIDE'S FALLBACK (D-78): when one side's language was verified and the
other side's quote failed, only the failed side is read from the script: its
own segments' profile gives it Arabic (mostly_ar), English (mostly_en), both
(mixed) or other, and the verified side keeps its code. With both quotes
failed, the whole call falls back as above.

THE SUMMARY LANGUAGE from the profile:

  mostly_ar  ar
  mostly_en  en
  mixed      whichever of Arabic and English has more words, counted over the
             segments spoken in it; a tie is ar
  other      en

Only a mixed call looks further, at words, because time favours whoever speaks
slowly. A word is a whitespace-separated token of a segment's text.

COACHED LANGUAGES (BRD B1): a call is coached and scored only when BOTH its
agent's and its client's languages are on the tenant's coaching_languages,
until each language is tested; otherwise both are null with
language_not_enabled (wave2.py). With either side heard, both must be heard
and listed: a side not heard is not a listed one. With neither heard, the
script fallback's profile decides: mostly_en needs en listed, mostly_ar an
Arabic code, mixed both, other never. A side read from its script is listed
by the same rule, on its own segments' profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from dodeal_ai.units.call_intelligence.prompts import said_by
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
# The languages coached by default: every Arabic code and English.
DEFAULT_COACHING_LANGUAGES: frozenset[CoachedLanguage] = frozenset(
    {
        "gulf_ar",
        "egyptian_ar",
        "levantine_ar",
        "iraqi_ar",
        "maghrebi_ar",
        "msa_ar",
        "en",
    }
)
# Why a call gets no coaching and no score (BRD B1).
LANGUAGE_NOT_ENABLED = "language_not_enabled"

# Where the spoken languages came from, for the stage-1 languages block: one
# side from the model and the null one from its own script is the third.
FROM_MODEL = "model"
FROM_SCRIPT = "script"
FROM_MODEL_AND_SCRIPT = "model_and_script"
SIDES = ("client", "agent")

# The language families each profile stands for, the coaching gate's unit.
_FAMILIES: dict[LanguageProfile, frozenset[str]] = {
    LanguageProfile.MOSTLY_AR: frozenset({"ar"}),
    LanguageProfile.MOSTLY_EN: frozenset({ENGLISH}),
    LanguageProfile.MIXED: frozenset({"ar", ENGLISH}),
    LanguageProfile.OTHER: frozenset({OTHER}),
}


@dataclass(frozen=True, slots=True)
class Spoken:
    """Each side's language as the roles pass heard it; None when unheard.
    `script` names the one side whose quote failed while the other's held:
    that side is read from its own segments' script (D-78)."""

    client: str | None = None
    agent: str | None = None
    script: str | None = None

    @property
    def heard(self) -> bool:
        return self.client is not None or self.agent is not None


UNHEARD = Spoken()


def _family(code: str) -> str:
    """ar for an Arabic dialect, en for English, other for anything else."""
    if code in ARABIC_LANGUAGES:
        return "ar"
    return ENGLISH if code == ENGLISH else OTHER


def _side_families(segments: tuple[Segment, ...], side: str) -> frozenset[str]:
    """The families one side's own segments' script gives it."""
    own = tuple(segment for segment in segments if said_by(segment) == side)
    return _FAMILIES[profile_of(own)]


def call_profile(transcript: Transcript, spoken: Spoken = UNHEARD) -> LanguageProfile:
    """The call's language profile: from the spoken languages when a side was
    heard, else the transcript's own (the module docstring's tables)."""
    if not spoken.heard:
        return transcript.language_profile
    families = {_family(code) for code in (spoken.client, spoken.agent) if code}
    if spoken.script is not None:
        families |= _side_families(transcript.segments, spoken.script)
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
    if not spoken.heard:
        return _listed(_FAMILIES[profile_of(segments)], enabled)
    return all(
        _listed(_side_families(segments, side), enabled)
        if side == spoken.script
        else getattr(spoken, side) in enabled
        for side in SIDES
    )


def _listed(families: frozenset[str], enabled: frozenset[str]) -> bool:
    """Every family listed: ar by any Arabic code, en by en, other never."""
    arabic = bool(enabled & ARABIC_LANGUAGES)
    return all(
        (family == "ar" and arabic) or (family == ENGLISH and ENGLISH in enabled)
        for family in families
    )


def languages_block(transcript: Transcript, spoken: Spoken) -> dict[str, object]:
    """Stage 1's languages block: each side's code, the profile, and whether
    they came from the model, the script fallback, or one side from each."""
    source = (
        FROM_SCRIPT
        if not spoken.heard
        else FROM_MODEL
        if spoken.script is None
        else FROM_MODEL_AND_SCRIPT
    )
    return {
        "client": spoken.client,
        "agent": spoken.agent,
        "profile": call_profile(transcript, spoken).value,
        "source": source,
    }


def spoken_of(block: object) -> Spoken:
    """The spoken languages back from a stored languages block; unheard for
    a block that is missing, from the fallback, or holds an unknown code. One
    side from each gives the null side back to its script, and needs exactly
    one side null."""
    if not isinstance(block, dict):
        return UNHEARD
    source = block.get("source")
    if source not in (FROM_MODEL, FROM_MODEL_AND_SCRIPT):
        return UNHEARD
    sides = [block.get("client"), block.get("agent")]
    if not all(side is None or side in CALL_LANGUAGES for side in sides):
        return UNHEARD
    client, agent = sides
    script = None
    if source == FROM_MODEL_AND_SCRIPT:
        unheard = [
            name for name, code in zip(SIDES, sides, strict=True) if code is None
        ]
        if len(unheard) != 1:
            return UNHEARD
        script = unheard[0]
    return Spoken(
        client=client if isinstance(client, str) else None,
        agent=agent if isinstance(agent, str) else None,
        script=script,
    )
