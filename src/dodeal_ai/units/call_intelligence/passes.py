"""Wave 1's two model passes (Unit B), on Unit A's call pattern: one call each
through llm_call.call_model, the calls budget charged, temperature 0 unless a
profile says otherwise, one reprompt with Unit B's tail on a malformed answer,
and a fixed output ceiling sized for a non-reasoning model.

  unit_b.extract  the six elements, six details and the client's mood
  unit_b.prose    a summary and a CRM note, in the language decided in code

EVIDENCE FOR EVERY ELEMENT (extract_v2). What the client wanted, each concern
and each agreement carry the quote and segment they rest on, and so does the
next step whenever it names an action. Each detail and the mood may cite one.
Every quote goes through the quote check (evidence.py); a missing or failing
quote is a malformed answer: the pass is reprompted once, then fails.

WHAT THE MODEL MAY NOT DECIDE. A detail not mentioned has no value, quote or
segment, and one stated has all three: either broken is malformed, never
repaired. A detail stated from a segment below the transcript's confidence
floor is marked uncertain in code, and every detail of an uncertain
transcript is (settled()). The CRM note is at most 80 words, and the summary
and note must be written in the language asked for -- at least 60 % of their
letters outside quotes in its script (evidence.in_language); both are checked
here.

The prose pass reads the transcript and the SETTLED extraction -- validated
and quote-checked output, in the data half and neutralised like the
transcript -- never a rejected answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_EXTRACT, PROFILE_UNIT_B_PROSE
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Cited,
    Errors,
    Quote,
    Said,
    SegmentId,
    Strict,
    evidence_errors,
    in_language,
    quote_errors,
)
from dodeal_ai.units.call_intelligence.prompts import (
    EXTRACT_TEMPLATE,
    PROSE_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    build_call_prompt,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

EXTRACT_LABEL = "llm.unit_b.extract"
PROSE_LABEL = "llm.unit_b.prose"

# What each answer may cost (register item 15), sized against the longest
# ARABIC answer: the extraction is six elements with their quotes, six details
# with a quote each and the mood; the prose is six sentences and an 80-word
# note. Register item 116: a reasoning model would spend hidden tokens here.
EXTRACT_MAX_OUTPUT_TOKENS = 2500
PROSE_MAX_OUTPUT_TOKENS = 1500

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

type _Item = Annotated[str, Field(min_length=1, max_length=_ITEM_CHARS)]
type _Items = Annotated[list[_Item], Field(max_length=_ITEMS)]


class Detail(Strict):
    value: Annotated[str | None, Field(max_length=_ITEM_CHARS)]
    state: Literal["stated", "not_mentioned", "uncertain"]
    quote: Quote
    segment: SegmentId


class Details(Strict):
    budget: Detail
    area: Detail
    property_reference: Detail
    timeline: Detail
    payment_method: Detail
    decision_maker: Detail


class Item(Strict):
    """A concern or an agreement, and the quote it rests on."""

    text: _Item
    quote: Said
    segment: Cited


class Wanted(Strict):
    """What the client wants, in one sentence, and the quote it rests on."""

    text: Said
    quote: Said
    segment: Cited


class NextStep(Strict):
    """The next action, and the quote it rests on whenever there is one."""

    action: Annotated[str | None, Field(max_length=_SENTENCE_CHARS)]
    owner: Literal["agent", "client", "unknown"]
    due: Annotated[str | None, Field(max_length=_ITEM_CHARS)]
    quote: Quote
    segment: SegmentId


class Mood(Strict):
    value: Literal["positive", "neutral", "negative"]
    quote: Quote
    segment: SegmentId


class Extraction(Strict):
    """unit_b.extract's answer, exactly (extract_v2)."""

    wanted: Wanted | None
    discussed: _Items
    concerns: Annotated[list[Item], Field(max_length=_ITEMS)]
    agreed: Annotated[list[Item], Field(max_length=_ITEMS)]
    next_step: NextStep
    ending: Literal["moved_forward", "stalled", "needs_follow_up", "dead"]
    details: Details
    mood: Mood


class Prose(Strict):
    """unit_b.prose's answer, exactly."""

    summary: Annotated[str, Field(min_length=1, max_length=_SUMMARY_CHARS)]
    crm_note: Annotated[str, Field(min_length=1, max_length=_NOTE_CHARS)]


def _element_errors(call: CallText, answer: Extraction) -> Errors:
    """The quote check on each summary element's evidence."""
    errors: Errors = []
    if answer.wanted is not None:
        errors += quote_errors(
            call, "wanted", answer.wanted.quote, answer.wanted.segment
        )
    for field in ("concerns", "agreed"):
        items: list[Item] = getattr(answer, field)
        for n, item in enumerate(items):
            errors += quote_errors(call, f"{field}.{n}", item.quote, item.segment)
    step = answer.next_step
    check = quote_errors if step.action is None else evidence_errors
    errors += check(call, "next_step", step.quote, step.segment)
    return errors


def check_extraction(call: CallText) -> Callable[[Extraction], None]:
    """The rules the schema cannot hold (module docstring), for call_model."""

    def check(answer: Extraction) -> None:
        errors: Errors = []
        for name in DETAIL_NAMES:
            detail: Detail = getattr(answer.details, name)
            where = f"details.{name}"
            given = (detail.value, detail.quote, detail.segment)
            if detail.state == NOT_MENTIONED and given != (None, None, None):
                errors.append((where, "not_mentioned_with_value"))
            if detail.state == STATED and None in given:
                errors.append((where, "stated_without_quote"))
            errors += quote_errors(call, where, detail.quote, detail.segment)
        errors += quote_errors(call, "mood", answer.mood.quote, answer.mood.segment)
        errors += _element_errors(call, answer)
        if errors:
            raise output_rejected(EXTRACT_LABEL, tuple(errors))

    return check


def check_prose(call: CallText) -> Callable[[Prose], None]:
    """The CRM note's length, and both texts in the language asked for."""

    def check(answer: Prose) -> None:
        errors: Errors = []
        if len(answer.crm_note.split()) > MAX_CRM_NOTE_WORDS:
            errors.append(("crm_note", "crm_note_too_long"))
        for field, text in (("summary", answer.summary), ("crm_note", answer.crm_note)):
            if not in_language(text, call.language):
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
        tail_by_error=REPROMPT_TAILS,
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
        tail_by_error=REPROMPT_TAILS,
    )
