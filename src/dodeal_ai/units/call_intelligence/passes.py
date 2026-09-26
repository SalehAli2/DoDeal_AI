"""Wave 1's two model passes (Unit B), on Unit A's call pattern: one call each
through llm_call.call_model, the calls budget charged, temperature 0 unless a
profile says otherwise, one reprompt with Unit B's tail on a malformed answer,
and a fixed output ceiling sized for a non-reasoning model.

  unit_b.extract  the six elements, six details and the client's mood
  unit_b.prose    a summary and a CRM note, in the language decided in code

EVIDENCE FOR EVERY ELEMENT (extract_v2). What the client wanted, each concern
and each agreement carry the quote and segment they rest on, and so does the
next step whenever it names an action; a stated detail owes one. Each other
detail and the mood may cite one. Every quote goes through the quote check
(evidence.py), FIELD BY FIELD: a detail whose quote fails is kept uncertain,
its quote and segment null and evidence_failed true; an element whose quote
fails is kept unverified, its quote and segment null; a mood whose quote fails
is kept uncertain the same way (settled()). The answer is malformed -- one
reprompt, then the pass fails -- only on a broken shape, or when more than
half of the quotes it gives or owes fail.

WHAT THE MODEL MAY NOT DECIDE. A detail not mentioned has no value, quote or
segment, and one stated has a value: either broken is malformed, never
repaired. A detail stated from a segment below the transcript's confidence
floor is marked uncertain in code, and every detail of an uncertain
transcript is (settled()). The CRM note is at most 80 words, and the summary
and note must be written in the language asked for -- at least 60 % of their
letters outside quotes in its script (evidence.in_language); both are checked
here.

The prose pass reads the transcript and the SETTLED extraction -- validated
and quote-checked output with every failed quote removed, in the data half
and neutralised like the transcript -- never a rejected answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

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
    relocated,
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

# The share of an extraction's quotes that may fail and the answer still be
# kept field by field: more than half failing is a model not reading the call.
# Lower reprompts answers with one slip; higher keeps answers mostly invented.
MAX_FAILED_QUOTE_SHARE = 0.5

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


def _detail_quote(call: CallText, where: str, detail: Detail) -> Errors | None:
    """A detail's quote check; None when it neither gives nor owes one."""
    if detail.state == STATED:
        return evidence_errors(call, where, detail.quote, detail.segment)
    if detail.state == UNCERTAIN and (detail.quote, detail.segment) != (None, None):
        return quote_errors(call, where, detail.quote, detail.segment)
    return None


def extraction_quotes(call: CallText, answer: Extraction) -> dict[str, Errors]:
    """Every quote the extraction gives or owes, by where it is, with the quote
    check's failures ([] for one that passes)."""
    found: dict[str, Errors] = {}
    for name in DETAIL_NAMES:
        checked = _detail_quote(call, f"details.{name}", getattr(answer.details, name))
        if checked is not None:
            found[f"details.{name}"] = checked
    mood = answer.mood
    if (mood.quote, mood.segment) != (None, None):
        found["mood"] = quote_errors(call, "mood", mood.quote, mood.segment)
    if answer.wanted is not None:
        wanted = answer.wanted
        found["wanted"] = quote_errors(call, "wanted", wanted.quote, wanted.segment)
    for field in ("concerns", "agreed"):
        items: list[Item] = getattr(answer, field)
        for n, item in enumerate(items):
            where = f"{field}.{n}"
            found[where] = quote_errors(call, where, item.quote, item.segment)
    step = answer.next_step
    if step.action is not None or (step.quote, step.segment) != (None, None):
        check = quote_errors if step.action is None else evidence_errors
        found["next_step"] = check(call, "next_step", step.quote, step.segment)
    return found


def _shape_errors(answer: Extraction) -> Errors:
    """What no quote can mend: a detail not mentioned that carries anything, a
    stated one with no value."""
    errors: Errors = []
    for name in DETAIL_NAMES:
        detail: Detail = getattr(answer.details, name)
        where = f"details.{name}"
        given = (detail.value, detail.quote, detail.segment)
        if detail.state == NOT_MENTIONED and given != (None, None, None):
            errors.append((where, "not_mentioned_with_value"))
        if detail.state == STATED and detail.value is None:
            errors.append((where, "stated_without_value"))
    return errors


def check_extraction(call: CallText) -> Callable[[Extraction], None]:
    """The rules the schema cannot hold (module docstring), for call_model:
    malformed on a broken shape or when more than half of the quotes fail."""

    def check(answer: Extraction) -> None:
        quotes = extraction_quotes(call, answer)
        failed = [error for errors in quotes.values() for error in errors]
        failing = sum(1 for errors in quotes.values() if errors)
        shape = _shape_errors(answer)
        if shape or failing > MAX_FAILED_QUOTE_SHARE * len(quotes):
            raise output_rejected(EXTRACT_LABEL, tuple(shape + failed))

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


def _settled_detail(call: CallText, detail: Detail, failed: bool) -> dict[str, object]:
    """A detail as delivered: a failed quote removed and the detail uncertain;
    uncertain too on an uncertain transcript or a low-confidence segment."""
    kept = detail.model_dump(mode="json")
    doubtful = call.uncertain or (
        detail.state == STATED and call.low_confidence(detail.segment)
    )
    if failed:
        kept.update(quote=None, segment=None)
    if failed or doubtful:
        kept["state"] = UNCERTAIN
    return {**kept, "evidence_failed": failed}


def _settled_item(item: BaseModel, failed: bool) -> dict[str, object]:
    """An element as delivered: kept with unverified true and no quote when its
    quote failed."""
    kept = item.model_dump(mode="json")
    if failed:
        kept.update(quote=None, segment=None)
    return {**kept, "unverified": failed}


def settled(answer: Extraction, call: CallText) -> dict[str, Any]:
    """The extraction as stage 1 delivers it and the prose pass reads it, with
    code's word on evidence and certainty (module docstring). Values are never
    touched: a detail not mentioned stays null."""
    failed = {
        where for where, errors in extraction_quotes(call, answer).items() if errors
    }
    mood = answer.mood
    mood_failed = "mood" in failed
    return {
        "wanted": (
            None
            if answer.wanted is None
            else _settled_item(answer.wanted, "wanted" in failed)
        ),
        "discussed": list(answer.discussed),
        **{
            field: [
                _settled_item(item, f"{field}.{n}" in failed)
                for n, item in enumerate(getattr(answer, field))
            ]
            for field in ("concerns", "agreed")
        },
        "next_step": _settled_item(answer.next_step, "next_step" in failed),
        "ending": answer.ending,
        "details": {
            name: _settled_detail(
                call, getattr(answer.details, name), f"details.{name}" in failed
            )
            for name in DETAIL_NAMES
        },
        "mood": {
            "value": mood.value,
            "quote": None if mood_failed else mood.quote,
            "segment": None if mood_failed else mood.segment,
            "uncertain": (
                mood_failed or call.uncertain or call.low_confidence(mood.segment)
            ),
            "evidence_failed": mood_failed,
        },
    }


async def extract(
    client: LLMClient, call: CallText, *, scope: TenantScope, settings: Settings
) -> tuple[Extraction, LLMResponse]:
    """unit_b.extract: one call, or two when the first answer is malformed."""
    answer, response = await call_model(
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
    return relocated(call, answer), response


async def write_prose(
    client: LLMClient,
    call: CallText,
    extraction: dict[str, Any],
    *,
    scope: TenantScope,
    settings: Settings,
) -> tuple[Prose, LLMResponse]:
    """unit_b.prose from the transcript and the settled extraction."""
    shown = json.dumps(extraction, ensure_ascii=False, sort_keys=True)
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
