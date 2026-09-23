"""Wave 1's two model passes (Unit B), on Unit A's call pattern: one call each
through llm_call.call_model, the calls budget charged, temperature 0 unless a
profile says otherwise, one reprompt with Unit B's tail on a malformed answer,
and a fixed output ceiling sized for a non-reasoning model.

  unit_b.extract  the six elements, six details and the client's mood
  unit_b.prose    a summary and a CRM note, in the language decided in code

THE QUOTE CHECK, in code, on every quote the extraction returns: at most 25
words; the segment it cites exists; and its words, normalised as the alarm
matcher normalises (alarms.py), appear in that segment in order and unbroken.
The segment is read as the model read it -- the prompt copy, numbers and
emails masked -- so a quote can never carry a number back out. Any failure is
a malformed answer: the pass is reprompted once, then fails.

WHAT THE MODEL MAY NOT DECIDE. A detail not mentioned has no value, quote or
segment, and one stated has all three: either broken is malformed, never
repaired. A detail stated from a segment below the transcript's confidence
floor is marked uncertain in code, and every detail of an uncertain
transcript is (settled()). The CRM note is at most 80 words, and the summary
and note must be written in the language asked for; both are checked here.

The prose pass reads the transcript and the SETTLED extraction -- validated
and quote-checked output, in the data half and neutralised like the
transcript -- never a rejected answer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_EXTRACT, PROFILE_UNIT_B_PROSE
from dodeal_ai.units.call_intelligence.alarms import words
from dodeal_ai.units.call_intelligence.language import (
    SummaryLanguage,
    summary_language,
)
from dodeal_ai.units.call_intelligence.numbers import prompt_copy
from dodeal_ai.units.call_intelligence.prompts import (
    EXTRACT_TEMPLATE,
    PROSE_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    build_call_prompt,
    render_transcript,
)
from dodeal_ai.units.call_intelligence.transcriber import (
    MIN_MEAN_CONFIDENCE,
    Segment,
    Transcript,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

EXTRACT_LABEL = "llm.unit_b.extract"
PROSE_LABEL = "llm.unit_b.prose"

# What each answer may cost (register item 15), sized against the longest
# ARABIC answer: the extraction is six elements, six details with a quote each
# and the mood; the prose is six sentences and an 80-word note. Register item
# 116: a reasoning model would spend hidden tokens from these and truncate.
EXTRACT_MAX_OUTPUT_TOKENS = 2500
PROSE_MAX_OUTPUT_TOKENS = 1500

MAX_QUOTE_WORDS = 25
MAX_CRM_NOTE_WORDS = 80

STATED = "stated"
NOT_MENTIONED = "not_mentioned"
UNCERTAIN = "uncertain"

DETAIL_NAMES = (
    "budget",
    "area",
    "property_reference",
    "timeline",
    "payment_method",
    "decision_maker",
)

# Lengths past any honest answer: a field the size of the transcript is not one.
_SENTENCE_CHARS = 400
_ITEM_CHARS = 200
_ITEMS = 6
_SUMMARY_CHARS = 2000
_NOTE_CHARS = 1000
_SEGMENT = r"^s[1-9][0-9]{0,4}$"

type _Item = Annotated[str, Field(min_length=1, max_length=_ITEM_CHARS)]
type _Items = Annotated[list[_Item], Field(max_length=_ITEMS)]
type _Quote = Annotated[str | None, Field(max_length=_SENTENCE_CHARS)]
type _SegmentId = Annotated[str | None, Field(pattern=_SEGMENT)]

_ARABIC_LETTER = re.compile(r"[ء-ي]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Detail(_Strict):
    value: Annotated[str | None, Field(max_length=_ITEM_CHARS)]
    state: Literal["stated", "not_mentioned", "uncertain"]
    quote: _Quote
    segment: _SegmentId


class Details(_Strict):
    budget: Detail
    area: Detail
    property_reference: Detail
    timeline: Detail
    payment_method: Detail
    decision_maker: Detail


class NextStep(_Strict):
    action: Annotated[str | None, Field(max_length=_SENTENCE_CHARS)]
    owner: Literal["agent", "client", "unknown"]
    due: Annotated[str | None, Field(max_length=_ITEM_CHARS)]


class Mood(_Strict):
    value: Literal["positive", "neutral", "negative"]
    quote: _Quote
    segment: _SegmentId


class Extraction(_Strict):
    """unit_b.extract's answer, exactly."""

    wanted: Annotated[str | None, Field(max_length=_SENTENCE_CHARS)]
    discussed: _Items
    concerns: _Items
    agreed: _Items
    next_step: NextStep
    ending: Literal["moved_forward", "stalled", "needs_follow_up", "dead"]
    details: Details
    mood: Mood


class Prose(_Strict):
    """unit_b.prose's answer, exactly."""

    summary: Annotated[str, Field(min_length=1, max_length=_SUMMARY_CHARS)]
    crm_note: Annotated[str, Field(min_length=1, max_length=_NOTE_CHARS)]


@dataclass(frozen=True, slots=True)
class CallText:
    """The transcript as both passes see it: each segment's prompt copy, the
    summary language, and whether the transcript as a whole is uncertain."""

    segments: tuple[Segment, ...]
    shown: tuple[str, ...]
    language: SummaryLanguage
    uncertain: bool

    @classmethod
    def of(cls, transcript: Transcript, *, country_code: str) -> CallText:
        """`country_code` is the tenant's, which a local number is masked under."""
        return cls(
            segments=transcript.segments,
            shown=tuple(
                prompt_copy(segment.text, country_code=country_code)
                for segment in transcript.segments
            ),
            language=summary_language(transcript),
            uncertain=transcript.uncertain,
        )

    def data(self) -> str:
        """The transcript first, then the language line."""
        transcript = render_transcript(self.segments, self.shown)
        return f"TRANSCRIPT:\n{transcript}\n\nLANGUAGE: {self.language}"

    def index_of(self, segment: str) -> int | None:
        """The position of the segment an id names, or None for none."""
        index = int(segment[1:]) - 1
        return index if index < len(self.segments) else None

    def low_confidence(self, segment: str | None) -> bool:
        index = None if segment is None else self.index_of(segment)
        return index is not None and (
            self.segments[index].confidence < MIN_MEAN_CONFIDENCE
        )


def _contains(said: Sequence[str], quote: Sequence[str]) -> bool:
    size = len(quote)
    return any(said[at : at + size] == quote for at in range(len(said) - size + 1))


def _quote_errors(
    call: CallText, where: str, quote: str | None, segment: str | None
) -> list[tuple[str, str]]:
    """The quote check for one cited quote; [] when it passes."""
    if quote is None and segment is None:
        return []
    if quote is None or segment is None:
        return [(where, "quote_without_segment")]
    index = call.index_of(segment)
    if index is None:
        return [(where, "segment_unknown")]
    quoted = words(quote)
    if not quoted or len(quoted) > MAX_QUOTE_WORDS:
        return [(where, "quote_length")]
    if not _contains(words(call.shown[index]), quoted):
        return [(where, "quote_not_in_segment")]
    return []


def check_extraction(call: CallText) -> Callable[[Extraction], None]:
    """The rules the schema cannot hold (module docstring), for call_model."""

    def check(answer: Extraction) -> None:
        errors: list[tuple[str, str]] = []
        for name in DETAIL_NAMES:
            detail: Detail = getattr(answer.details, name)
            where = f"details.{name}"
            given = (detail.value, detail.quote, detail.segment)
            if detail.state == NOT_MENTIONED and given != (None, None, None):
                errors.append((where, "not_mentioned_with_value"))
            if detail.state == STATED and None in given:
                errors.append((where, "stated_without_quote"))
            errors += _quote_errors(call, where, detail.quote, detail.segment)
        errors += _quote_errors(call, "mood", answer.mood.quote, answer.mood.segment)
        if errors:
            raise output_rejected(EXTRACT_LABEL, tuple(errors))

    return check


def _in_language(text: str, language: SummaryLanguage) -> bool:
    letters = _ARABIC_LETTER if language == "ar" else _LATIN_LETTER
    return letters.search(text) is not None


def check_prose(call: CallText) -> Callable[[Prose], None]:
    """The CRM note's length, and both texts in the language asked for."""

    def check(answer: Prose) -> None:
        errors: list[tuple[str, str]] = []
        if len(answer.crm_note.split()) > MAX_CRM_NOTE_WORDS:
            errors.append(("crm_note", "crm_note_too_long"))
        for field, text in (("summary", answer.summary), ("crm_note", answer.crm_note)):
            if not _in_language(text, call.language):
                errors.append((field, "wrong_language"))
        if errors:
            raise output_rejected(PROSE_LABEL, tuple(errors))

    return check


def settled(answer: Extraction, call: CallText) -> Extraction:
    """The extraction with code's word on certainty: every detail uncertain on
    an uncertain transcript, and a stated one citing a low-confidence segment
    uncertain. Values are never touched: a detail not mentioned stays null."""
    changed = {}
    for name in DETAIL_NAMES:
        detail: Detail = getattr(answer.details, name)
        doubtful = call.uncertain or (
            detail.state == STATED and call.low_confidence(detail.segment)
        )
        if doubtful and detail.state != UNCERTAIN:
            changed[name] = detail.model_copy(update={"state": UNCERTAIN})
    details = answer.details.model_copy(update=changed)
    return answer.model_copy(update={"details": details})


def mood_uncertain(answer: Extraction, call: CallText) -> bool:
    """The mood is uncertain on an uncertain transcript or a doubtful segment."""
    return call.uncertain or call.low_confidence(answer.mood.segment)


async def extract(
    client: LLMClient, call: CallText, *, scope: TenantScope, settings: Settings
) -> tuple[Extraction, LLMResponse]:
    """unit_b.extract: one call, or two when the first answer is malformed."""
    return await call_model(
        client,
        build_call_prompt(EXTRACT_TEMPLATE, call.data()),
        Extraction,
        EXTRACT_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_B_EXTRACT,
        max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
        check=check_extraction(call),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
    )


async def write_prose(
    client: LLMClient,
    call: CallText,
    extraction: Extraction,
    *,
    scope: TenantScope,
    settings: Settings,
) -> tuple[Prose, LLMResponse]:
    """unit_b.prose from the transcript and the settled extraction."""
    shown = json.dumps(
        extraction.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    return await call_model(
        client,
        build_call_prompt(PROSE_TEMPLATE, f"{call.data()}\n\nEXTRACTION:\n{shown}"),
        Prose,
        PROSE_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_B_PROSE,
        max_output_tokens=PROSE_MAX_OUTPUT_TOKENS,
        check=check_prose(call),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
    )
