"""Wave 1's two model passes (Unit B), on Unit A's call pattern: one call each
through llm_call.call_model, the calls budget charged, temperature 0 unless a
profile says otherwise, one reprompt with Unit B's tail on a malformed answer,
and a fixed output ceiling sized for a non-reasoning model.

  unit_b.extract  the six elements, eight details and the client's mood; the
                  details are budget, area, property_reference, timeline,
                  payment_method, decision_maker, property_status (off_plan,
                  ready or unknown) and handover_date (as said, and an ISO
                  date when the words name one)
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

A DEAD CALL'S LOSS REASON. An ending of dead carries loss_reason, and no
other ending does: one of the nine objection categories, owed the client's
quote, or no_reason_given, quoted when the client said something; a quote
that fails is kept unverified as an element's is.

THE NEXT STEP'S TIME. An action has a kind (viewing, online_meeting,
office_visit, callback, send_details, other). Its time is resolved by the
model from the call's recorded_at and the tenant's zone, both in the data half
(CallClock), as ISO 8601 with an offset; code refuses one before the call or
more than MAX_NEXT_STEP_DAYS after it, and delivers the rest in the tenant's
zone. Vague or refused timing is null with when_state uncertain; booked holds
only with a kept time on verified evidence.

The prose pass reads the transcript and the SETTLED extraction -- validated
and quote-checked output with every failed quote removed, in the data half
and neutralised like the transcript -- never a rejected answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, Field, model_validator

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
    failed_quotes,
    in_language,
    mostly_failed,
    quote_errors,
    relocated,
)
from dodeal_ai.units.call_intelligence.objections import OBJECTION_CATEGORIES
from dodeal_ai.units.call_intelligence.prompts import (
    CLIENT,
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
# ARABIC answer: the extraction is six elements with their quotes, eight details
# with a quote each, the next step's time and the mood; the prose is six
# sentences and an 80-word note. Register item 116: a reasoning model would
# spend hidden tokens here. Too low cuts an answer off: malformed, reprompted.
EXTRACT_MAX_OUTPUT_TOKENS = 3000
PROSE_MAX_OUTPUT_TOKENS = 1500

MAX_CRM_NOTE_WORDS = 80

STATED = "stated"
NOT_MENTIONED = "not_mentioned"
UNCERTAIN = "uncertain"

DEAD = "dead"
# A dead call whose client gave no reason: the one loss reason owing no quote.
NO_REASON_GIVEN = "no_reason_given"
LOSS_CATEGORIES = (*OBJECTION_CATEGORIES, NO_REASON_GIVEN)

DETAIL_NAMES = (
    "budget",
    "area",
    "property_reference",
    "timeline",
    "payment_method",
    "decision_maker",
    "property_status",
    "handover_date",
)

# A property status the call does not settle: the value not mentioned carries.
UNKNOWN_STATUS = "unknown"

# The furthest after the call a next step's time may be. A quarter ahead is a
# plan; later is more likely misheard or invented. Longer keeps such times as
# booked; shorter drops true ones to uncertain.
MAX_NEXT_STEP_DAYS = 90

# Lengths past any honest answer: a field the size of the transcript is not one.
_SENTENCE_CHARS = 400
_ITEM_CHARS = 200
_ITEMS = 6
_SUMMARY_CHARS = 2000
_NOTE_CHARS = 1000

type _Item = Annotated[str, Field(min_length=1, max_length=_ITEM_CHARS)]


class Detail(Strict):
    value: Annotated[str | None, Field(max_length=_ITEM_CHARS)]
    state: Literal["stated", "not_mentioned", "uncertain"]
    quote: Quote
    segment: SegmentId


class StatusDetail(Strict):
    """Whether the property is built: off_plan or ready, said with a quote;
    unknown when the call does not say."""

    value: Literal["off_plan", "ready", "unknown"]
    state: Literal["stated", "not_mentioned", "uncertain"]
    quote: Quote
    segment: SegmentId


class HandoverDetail(Strict):
    """When the property is handed over, as said, and as an ISO date when the
    words name one."""

    value: Annotated[str | None, Field(max_length=_ITEM_CHARS)]
    date: date | None
    state: Literal["stated", "not_mentioned", "uncertain"]
    quote: Quote
    segment: SegmentId


type AnyDetail = Detail | StatusDetail | HandoverDetail

# An answer kept before the two existed reads back as neither mentioned.
_STATUS_NOT_MENTIONED = StatusDetail(
    value="unknown", state="not_mentioned", quote=None, segment=None
)
_HANDOVER_NOT_MENTIONED = HandoverDetail(
    value=None, date=None, state="not_mentioned", quote=None, segment=None
)


class Details(Strict):
    budget: Detail
    area: Detail
    property_reference: Detail
    timeline: Detail
    payment_method: Detail
    decision_maker: Detail
    property_status: StatusDetail = _STATUS_NOT_MENTIONED
    handover_date: HandoverDetail = _HANDOVER_NOT_MENTIONED


class Item(Strict):
    """A concern or an agreement, and the quote it rests on."""

    text: _Item
    quote: Said
    segment: Cited


class Discussed(Strict):
    """A topic the call covered, and the quote it rests on; one with no quote
    is kept unverified (settled())."""

    text: _Item
    quote: Quote
    segment: SegmentId

    @model_validator(mode="before")
    @classmethod
    def _bare_topic(cls, value: object) -> object:
        """A bare topic -- as an answer kept before topics were quoted holds
        them -- is one with no quote."""
        if isinstance(value, str):
            return {"text": value, "quote": None, "segment": None}
        return value


class Wanted(Strict):
    """What the client wants, in one sentence, and the quote it rests on."""

    text: Said
    quote: Said
    segment: Cited


type NextKind = Literal[
    "viewing", "online_meeting", "office_visit", "callback", "send_details", "other"
]


class NextStep(Strict):
    """The next action, and the quote it rests on whenever there is one; its
    kind, its time resolved to ISO 8601, and whether both sides agreed it."""

    action: Annotated[str | None, Field(max_length=_SENTENCE_CHARS)]
    owner: Literal["agent", "client", "unknown"]
    due: Annotated[str | None, Field(max_length=_ITEM_CHARS)]
    quote: Quote
    segment: SegmentId
    # Defaults, so an answer kept before they existed still reads back.
    kind: NextKind | None = None
    when: AwareDatetime | None = None
    booked: bool = False


@dataclass(frozen=True, slots=True)
class CallClock:
    """When the call was recorded (None when the job does not say) and the
    tenant's time zone: what a next step's time is resolved from, by the
    model, and held to, in code."""

    recorded_at: datetime | None
    timezone: str

    def data(self) -> str:
        """The two lines the extraction's data half ends with."""
        recorded = (
            "unknown"
            if self.recorded_at is None
            else self.recorded_at.astimezone(ZoneInfo(self.timezone)).isoformat()
        )
        return f"RECORDED AT: {recorded}\nTIMEZONE: {self.timezone}"

    def held(self, when: datetime | None) -> datetime | None:
        """`when` in the tenant's zone; None when there is none, the call's own
        time is unknown, or it falls before the call or more than
        MAX_NEXT_STEP_DAYS after it."""
        if when is None or self.recorded_at is None:
            return None
        latest = self.recorded_at + timedelta(days=MAX_NEXT_STEP_DAYS)
        if not self.recorded_at <= when <= latest:
            return None
        return when.astimezone(ZoneInfo(self.timezone))


class Mood(Strict):
    value: Literal["positive", "neutral", "negative"]
    quote: Quote
    segment: SegmentId


# The nine objection categories (objections.py) and no_reason_given, as one
# flat list, so the schema a provider is sent is one enum.
type LossCategory = Literal[
    "price",
    "timing",
    "competitor",
    "trust",
    "property_fit",
    "payment_finance",
    "location",
    "third_party_approval",
    "service_charges_fees",
    "no_reason_given",
]


class LossReason(Strict):
    """Why a dead call died: one of the objection categories, or
    no_reason_given, with the client's quote when there is one."""

    category: LossCategory
    quote: Quote
    segment: SegmentId


class Extraction(Strict):
    """unit_b.extract's answer, exactly (extract_v2)."""

    wanted: Wanted | None
    discussed: Annotated[list[Discussed], Field(max_length=_ITEMS)]
    concerns: Annotated[list[Item], Field(max_length=_ITEMS)]
    agreed: Annotated[list[Item], Field(max_length=_ITEMS)]
    next_step: NextStep
    ending: Literal["moved_forward", "stalled", "needs_follow_up", "dead"]
    details: Details
    mood: Mood
    # A default, so an answer kept before it existed still reads back.
    loss_reason: LossReason | None = None


class Prose(Strict):
    """unit_b.prose's answer, exactly."""

    summary: Annotated[str, Field(min_length=1, max_length=_SUMMARY_CHARS)]
    crm_note: Annotated[str, Field(min_length=1, max_length=_NOTE_CHARS)]


def _detail_quote(call: CallText, where: str, detail: AnyDetail) -> Errors | None:
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
    for n, topic in enumerate(answer.discussed):
        where = f"discussed.{n}"
        found[where] = evidence_errors(call, where, topic.quote, topic.segment)
    for field in ("concerns", "agreed"):
        items: list[Item] = getattr(answer, field)
        for n, item in enumerate(items):
            where = f"{field}.{n}"
            found[where] = quote_errors(call, where, item.quote, item.segment)
    step = answer.next_step
    if step.action is not None or (step.quote, step.segment) != (None, None):
        check = quote_errors if step.action is None else evidence_errors
        found["next_step"] = check(call, "next_step", step.quote, step.segment)
    lost = answer.loss_reason
    given = lost is not None and (lost.quote, lost.segment) != (None, None)
    if lost is not None and (given or lost.category != NO_REASON_GIVEN):
        owed = quote_errors if lost.category == NO_REASON_GIVEN else evidence_errors
        found["loss_reason"] = owed(
            call, "loss_reason", lost.quote, lost.segment, speaker=CLIENT
        )
    return found


def _step_errors(step: NextStep) -> Errors:
    """An action has a kind; no action has no kind, time or booking."""
    if step.action is not None:
        return [] if step.kind is not None else [("next_step", "action_without_kind")]
    if (step.kind, step.when, step.booked) != (None, None, False):
        return [("next_step", "next_step_without_action")]
    return []


def _given(detail: AnyDetail) -> tuple[object, ...]:
    """What a detail carries: its value (none for an unknown status), its
    handover date, its quote and its segment."""
    value = None if detail.value == UNKNOWN_STATUS else detail.value
    when = detail.date if isinstance(detail, HandoverDetail) else None
    return (value, when, detail.quote, detail.segment)


def extraction_evidence(call: CallText, answer: Extraction) -> tuple[int, int]:
    """(dropped, unverified): the extraction drops nothing; every field whose
    quote failed is kept unverified or uncertain (settled())."""
    failing = sum(1 for errors in extraction_quotes(call, answer).values() if errors)
    return 0, failing


def _shape_errors(answer: Extraction) -> Errors:
    """What no quote can mend: a detail not mentioned that carries anything, a
    stated one with no value, a handover date with no words for it, a next
    step's kind without its action."""
    errors: Errors = _step_errors(answer.next_step)
    if answer.ending == DEAD and answer.loss_reason is None:
        errors.append(("loss_reason", "dead_without_loss_reason"))
    if answer.ending != DEAD and answer.loss_reason is not None:
        errors.append(("loss_reason", "loss_reason_without_dead"))
    for name in DETAIL_NAMES:
        detail: AnyDetail = getattr(answer.details, name)
        where = f"details.{name}"
        given = _given(detail)
        value, when = given[0], given[1]
        if detail.state == NOT_MENTIONED and given != (None, None, None, None):
            errors.append((where, "not_mentioned_with_value"))
        if detail.state == STATED and value is None:
            errors.append((where, "stated_without_value"))
        if when is not None and value is None:
            errors.append((where, "date_without_value"))
    return errors


def check_extraction(call: CallText) -> Callable[[Extraction], None]:
    """The rules the schema cannot hold (module docstring), for call_model:
    malformed on a broken shape or when more than half of the quotes fail."""

    def check(answer: Extraction) -> None:
        quotes = extraction_quotes(call, answer)
        shape = _shape_errors(answer)
        if shape or mostly_failed(quotes):
            raise output_rejected(EXTRACT_LABEL, tuple(shape + failed_quotes(quotes)))

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


def _settled_detail(
    call: CallText, detail: AnyDetail, failed: bool
) -> dict[str, object]:
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


def _settled_step(step: NextStep, failed: bool, clock: CallClock) -> dict[str, object]:
    """The next step as delivered: its time held to the call (CallClock.held)
    and in the tenant's zone; when_state stated for a held time on verified
    evidence, uncertain for a time said but vague, refused or unverified, else
    not_mentioned; booked only with a held time on verified evidence."""
    kept = _settled_item(step, failed)
    when = clock.held(step.when)
    timed = step.when is not None or step.due is not None
    state = NOT_MENTIONED if not timed else UNCERTAIN
    if when is not None and not failed:
        state = STATED
    return {
        **kept,
        "when": None if when is None else when.isoformat(),
        "when_state": state,
        "booked": step.booked and state == STATED,
    }


def settled(
    answer: Extraction, call: CallText, clock: CallClock | None = None
) -> dict[str, Any]:
    """The extraction as stage 1 delivers it and the prose pass reads it, with
    code's word on evidence and certainty (module docstring). Values are never
    touched: a detail not mentioned stays null. With no clock the call's time
    is unknown, so no next step's time is held."""
    clock = clock or CallClock(None, "UTC")
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
        "discussed": [
            _settled_item(topic, f"discussed.{n}" in failed)
            for n, topic in enumerate(answer.discussed)
        ],
        **{
            field: [
                _settled_item(item, f"{field}.{n}" in failed)
                for n, item in enumerate(getattr(answer, field))
            ]
            for field in ("concerns", "agreed")
        },
        "next_step": _settled_step(answer.next_step, "next_step" in failed, clock),
        "ending": answer.ending,
        "loss_reason": (
            None
            if answer.loss_reason is None
            else _settled_item(answer.loss_reason, "loss_reason" in failed)
        ),
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


def verified(extraction: dict[str, Any]) -> dict[str, Any]:
    """The settled extraction as the prose pass reads it: every element whose
    quote failed left out -- an unverified topic, concern or agreement from
    its list, an unverified wanted, next step or loss reason as null. The
    details keep their state: an uncertain one is written as uncertain."""
    kept = dict(extraction)
    for field in ("discussed", "concerns", "agreed"):
        kept[field] = [item for item in extraction[field] if not item["unverified"]]
    for field in ("wanted", "next_step", "loss_reason"):
        item = extraction[field]
        if item is not None and item["unverified"]:
            kept[field] = None
    return kept


async def extract(
    client: LLMClient,
    call: CallText,
    *,
    scope: TenantScope,
    settings: Settings,
    clock: CallClock | None = None,
) -> tuple[Extraction, LLMResponse]:
    """unit_b.extract: one call, or two when the first answer is malformed.
    The data half ends with the call's time and the tenant's zone (CallClock);
    with no clock, both unknown."""
    clock = clock or CallClock(None, "UTC")
    answer, response = await call_model(
        client,
        build_call_prompt(EXTRACT_TEMPLATE, f"{call.data()}\n\n{clock.data()}"),
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
    """unit_b.prose from the transcript and the settled extraction, its
    verified elements only (verified())."""
    shown = json.dumps(verified(extraction), ensure_ascii=False, sort_keys=True)
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
