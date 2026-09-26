"""unit_b.score (wave 2) and call_rubric_v2: the model answers yes-or-no checks
with quotes; code computes every mark, the total and the band.

THE RUBRIC, the BRD's weights, each component's checks:

  understanding    25  asked_budget, asked_timeline, asked_purpose,
                       asked_decision_maker, and listened_more (code: the
                       client's talk share at least 0.40)
  objections       25  from unit_b.objections (objections.py): (addressed +
                       satisfied) over (2 x raised); suppressed when none
                       was raised
  next_step        20  specific_commitment, has_date, named_owner
  professionalism  15  courteous, no_over_promise, no_pressure, and
                       not_interrupting (code: agent interruptions at most 2)
  product_knowledge 15 answered_directly, specific_details; suppressed when
                       the client asked no product question, and marked
                       correctness_unverified until a project feed exists

A component's mark is its weight split evenly over its checks, times the
checks passed, rounded half up; the total is the marks over the applicable
weight (the suppressed components' left out), times 100, rounded half up. The
band: excellent 85+, good 70 to 84, needs_work 50 to 69, coaching_required
below 50. Exact fractions throughout, so 12.5 is 13 and never 12.

A SCORE ONLY when the tenant's scoring_enabled is on, the client's language
is one it coaches (language.coached), the call is eligible, the client engaged
(a talk share of at least ENGAGED_SHARE, 0.15, or at least ENGAGED_TURNS, 5,
turns of ENGAGED_TURN_WORDS words or more; a tenant may set both numbers in
unit_b), and the objections pass answered; otherwise
null, with the reason: scoring_off, language_not_enabled, not_eligible,
not_engaged or objections_unavailable -- and the pass is not run.

THE EVIDENCE: each check's quote goes through the quote check; one is owed
wherever the answer says something was said -- every yes, but for
no_over_promise and no_pressure, where it is every no.

THE ESCALATIONS AGREE (D-75, call_rubric_v2). Once wave 2 has both parts, a
verified escalation the agent said answers the professionalism check it is an
exact match for (ESCALATION_CHECKS) no, source code, with its quote and
segment, and code marks the component, the total and the band again
(reconciled). Nothing changes for one the client or an unknown voice said,
one unverified, or with the escalations pass failed: the score stays strict.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, cast

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_SCORE, task_ceiling
from dodeal_ai.units.call_intelligence.escalations import OVER_PROMISE
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Errors,
    Quote,
    SegmentId,
    Strict,
    evidence_errors,
    quote_errors,
    relocated,
)
from dodeal_ai.units.call_intelligence.language import LANGUAGE_NOT_ENABLED
from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    SCORE_TEMPLATE,
    build_call_prompt,
    role_of,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

SCORE_LABEL = "llm.unit_b.score"
RUBRIC_VERSION = "call_rubric_v2"

# What the answer may cost (register item 15): thirteen checks with a quote
# each, sized against an Arabic call on a non-reasoning model.
SCORE_MAX_OUTPUT_TOKENS = 2500
# The same answer on a reasoning profile, whose hidden reasoning is spent
# inside the ceiling (register item 116): the ceiling follows the profile.
SCORE_REASONING_MAX_OUTPUT_TOKENS = 6000

# The client's talk share at which the agent listened more.
LISTENED_MORE_SHARE = 0.40
# The client engaged, so the call may be judged, at this talk share or up, or
# with this many turns of ENGAGED_TURN_WORDS words or more: a quiet buyer who
# still answered in sentences. The defaults of unit_b's engaged_share and
# engaged_turns (config.py); lower scores calls the client barely spoke on,
# higher leaves real but brief buyers unscored.
ENGAGED_SHARE = 0.15
ENGAGED_TURNS = 5
# The words a client turn needs to count toward ENGAGED_TURNS: a sentence,
# not "yes" or "okay thanks". Fixed here, not a tenant setting.
ENGAGED_TURN_WORDS = 4
# The most interruptions an agent may make and still not be interrupting.
MAX_AGENT_INTERRUPTIONS = 2

YES = "yes"
NO = "no"

# Why a call has no score.
SCORING_OFF = "scoring_off"
NOT_ELIGIBLE = "not_eligible"
NOT_ENGAGED = "not_engaged"
OBJECTIONS_UNAVAILABLE = "objections_unavailable"

# Why a component has no mark.
NO_OBJECTIONS = "no_objections"
NO_PRODUCT_QUESTION = "no_product_question"

# The bands, from the top: the first whose floor the total reaches.
BANDS = ((85, "excellent"), (70, "good"), (50, "needs_work"), (0, "coaching_required"))

# The checks whose quote is owed on a no: the promise, or the pressure.
_EVIDENCE_ON_NO = frozenset({"no_over_promise", "no_pressure"})

# The professionalism check each escalation type fails, exact matches only
# (D-75). rudeness_or_pressure is left out: it may be rudeness alone, which
# no_pressure does not judge, or pressure alone, which courteous does not.
ESCALATION_CHECKS = {OVER_PROMISE: "no_over_promise"}


class Check(Strict):
    """One yes-or-no check, and the quote that shows it."""

    answer: Literal["yes", "no"]
    quote: Quote
    segment: SegmentId


class ScoreChecks(Strict):
    """unit_b.score's answer, exactly."""

    asked_budget: Check
    asked_timeline: Check
    asked_purpose: Check
    asked_decision_maker: Check
    specific_commitment: Check
    has_date: Check
    named_owner: Check
    courteous: Check
    no_over_promise: Check
    no_pressure: Check
    product_question_asked: Check
    answered_directly: Check
    specific_details: Check


CHECK_NAMES: tuple[str, ...] = tuple(ScoreChecks.model_fields)


@dataclass(frozen=True, slots=True)
class Component:
    """One rubric component: its weight and the checks it is marked on."""

    name: str
    weight: int
    checks: tuple[str, ...]


UNDERSTANDING = Component(
    "understanding",
    25,
    (
        "asked_budget",
        "asked_timeline",
        "asked_purpose",
        "asked_decision_maker",
        "listened_more",
    ),
)
OBJECTIONS = Component("objections", 25, ())
NEXT_STEP = Component(
    "next_step", 20, ("specific_commitment", "has_date", "named_owner")
)
PROFESSIONALISM = Component(
    "professionalism",
    15,
    ("courteous", "no_over_promise", "no_pressure", "not_interrupting"),
)
PRODUCT_KNOWLEDGE = Component(
    "product_knowledge", 15, ("answered_directly", "specific_details")
)
RUBRIC = (UNDERSTANDING, OBJECTIONS, NEXT_STEP, PROFESSIONALISM, PRODUCT_KNOWLEDGE)


def check_score(call: CallText) -> Callable[[ScoreChecks], None]:
    """Each check's quote, owed where its answer says something was said."""

    def check(answer: ScoreChecks) -> None:
        errors: Errors = []
        for name in CHECK_NAMES:
            found: Check = getattr(answer, name)
            said = NO if name in _EVIDENCE_ON_NO else YES
            owed = evidence_errors if found.answer == said else quote_errors
            errors += owed(call, name, found.quote, found.segment)
        if errors:
            raise output_rejected(SCORE_LABEL, tuple(errors))

    return check


async def ask_checks(
    client: LLMClient, call: CallText, *, scope: TenantScope, settings: Settings
) -> tuple[ScoreChecks, LLMResponse]:
    """unit_b.score: one call, or two when the first answer is malformed."""
    answer, response = await call_model(
        client,
        build_call_prompt(SCORE_TEMPLATE, call.data()),
        ScoreChecks,
        SCORE_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_B_SCORE,
        max_output_tokens=task_ceiling(
            settings,
            PROFILE_UNIT_B_SCORE,
            plain=SCORE_MAX_OUTPUT_TOKENS,
            reasoning=SCORE_REASONING_MAX_OUTPUT_TOKENS,
            client=client,
        ),
        check=check_score(call),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=REPROMPT_TAILS,
    )
    return relocated(call, answer), response


def round_half_up(value: Fraction) -> int:
    """The nearest whole number, a half always up."""
    return math.floor(value + Fraction(1, 2))


def mark(weight: int, passed: int, of: int) -> int:
    """`weight` split evenly over `of` checks, times those passed."""
    return round_half_up(Fraction(weight * passed, of))


def band_of(total: int) -> str:
    """The band a total falls in."""
    return next(name for floor, name in BANDS if total >= floor)


def substantive_turns(segments: Sequence[Segment], role: str) -> int:
    """The role's turns of ENGAGED_TURN_WORDS words or more: a turn is a run
    of the role's segments with no other speaker between, its words the
    whitespace-separated tokens of them all."""
    turns = 0
    words = 0
    for n, segment in enumerate(segments):
        if role_of(segment) != role:
            continue
        words += len(segment.text.split())
        ends = n + 1 == len(segments) or role_of(segments[n + 1]) != role
        if ends:
            turns += 1 if words >= ENGAGED_TURN_WORDS else 0
            words = 0
    return turns


def engaged(
    client_share: float | None,
    client_turns: int,
    *,
    share: float = ENGAGED_SHARE,
    turns: int = ENGAGED_TURNS,
) -> bool:
    """Whether the client engaged: the share reached, or the turns. Never
    with no share at all: a call with no talk time has nothing to judge."""
    if client_share is None:
        return False
    return client_share >= share or client_turns >= turns


def score_gate(
    *,
    scoring_enabled: bool,
    language_enabled: bool,
    eligible: bool,
    client_share: float | None,
    objections_answered: bool,
    client_turns: int = 0,
    engaged_share: float = ENGAGED_SHARE,
    engaged_turns: int = ENGAGED_TURNS,
) -> str | None:
    """Why the call gets no score, or None when it gets one. `client_turns`
    is the client's substantive turns; the two thresholds are the tenant's."""
    if not scoring_enabled:
        return SCORING_OFF
    if not language_enabled:
        return LANGUAGE_NOT_ENABLED
    if not eligible:
        return NOT_ELIGIBLE
    if not engaged(
        client_share, client_turns, share=engaged_share, turns=engaged_turns
    ):
        return NOT_ENGAGED
    if not objections_answered:
        return OBJECTIONS_UNAVAILABLE
    return None


def _decided(answer: str) -> dict[str, object]:
    """A check decided in code, beside the model's."""
    return {"answer": answer, "source": "code"}


def _marked(
    component: Component, shown: dict[str, dict[str, object]]
) -> dict[str, object]:
    """A component marked on the checks shown: its weight over its checks,
    times the yes answers."""
    passed = sum(1 for check in shown.values() if check["answer"] == YES)
    return {
        "weight": component.weight,
        "mark": mark(component.weight, passed, len(component.checks)),
        "checks": shown,
    }


def _checked(
    component: Component, checks: ScoreChecks, code: dict[str, dict[str, object]]
) -> dict[str, object]:
    """A component marked on its checks: the model's, then code's."""
    shown: dict[str, dict[str, object]] = {}
    for name in component.checks:
        shown[name] = (
            code[name]
            if name in code
            else getattr(checks, name).model_dump(mode="json")
        )
    return _marked(component, shown)


def _totalled(components: dict[str, dict[str, object]]) -> dict[str, object]:
    """The total over the applicable weight, its band, the raw sum and that
    weight, from the components' marks (a suppressed one left out)."""
    marked = [c for c in components.values() if c["mark"] is not None]
    applicable = sum(int(str(c["weight"])) for c in marked)
    raw = sum(int(str(c["mark"])) for c in marked)
    total = round_half_up(Fraction(raw * 100, applicable))
    return {
        "total": total,
        "band": band_of(total),
        "raw": raw,
        "applicable_weight": applicable,
    }


def _objections(counts: dict[str, object]) -> dict[str, object]:
    """(addressed + satisfied) over (2 x raised), or suppressed with none."""
    raised = int(str(counts["raised"]))
    if raised == 0:
        return {"weight": OBJECTIONS.weight, "mark": None, "suppressed": NO_OBJECTIONS}
    earned = int(str(counts["addressed"])) + int(str(counts["satisfied"]))
    return {
        "weight": OBJECTIONS.weight,
        "mark": mark(OBJECTIONS.weight, earned, 2 * raised),
        "raised": raised,
        "addressed": counts["addressed"],
        "satisfied": counts["satisfied"],
    }


def score_call(
    checks: ScoreChecks,
    objections: dict[str, object],
    *,
    client_share: float,
    agent_interruptions: int,
) -> dict[str, object]:
    """The score part: each component, the total over the applicable weight,
    and its band. Every number here is code's."""
    code = {
        "listened_more": _decided(YES if client_share >= LISTENED_MORE_SHARE else NO),
        "not_interrupting": _decided(
            YES if agent_interruptions <= MAX_AGENT_INTERRUPTIONS else NO
        ),
    }
    product: dict[str, object] = (
        {
            **_checked(PRODUCT_KNOWLEDGE, checks, code),
            "correctness_unverified": True,
        }
        if checks.product_question_asked.answer == YES
        else {
            "weight": PRODUCT_KNOWLEDGE.weight,
            "mark": None,
            "suppressed": NO_PRODUCT_QUESTION,
        }
    )
    components: dict[str, dict[str, object]] = {
        UNDERSTANDING.name: _checked(UNDERSTANDING, checks, code),
        OBJECTIONS.name: _objections(objections),
        NEXT_STEP.name: _checked(NEXT_STEP, checks, code),
        PROFESSIONALISM.name: _checked(PROFESSIONALISM, checks, code),
        PRODUCT_KNOWLEDGE.name: product,
    }
    return {
        **_totalled(components),
        "product_question_asked": checks.product_question_asked.model_dump(mode="json"),
        "components": components,
    }


def _agents_verified(call: CallText, item: dict[str, object]) -> bool:
    """Whether an escalation rests on the agent's own words: said by the
    agent, not kept unverified, and its quote found again, in code, in an
    agent segment of this call."""
    quote, segment = item.get("quote"), item.get("segment")
    if item.get("speaker") != AGENT or item.get("unverified") is True:
        return False
    if not isinstance(quote, str) or not isinstance(segment, str):
        return False
    return not evidence_errors(call, "escalations", quote, segment, speaker=AGENT)


def escalated_checks(
    call: CallText, items: Sequence[dict[str, object]]
) -> dict[str, dict[str, object]]:
    """Each check an escalation answers (ESCALATION_CHECKS), decided no in
    code with the quote and segment of the first such escalation in time
    that is the agent's and verified (_agents_verified)."""
    answers: dict[str, dict[str, object]] = {}
    for item in items:
        check = ESCALATION_CHECKS.get(str(item.get("type")))
        if check is None or check in answers or not _agents_verified(call, item):
            continue
        answers[check] = {
            "answer": NO,
            "source": "code",
            "quote": item["quote"],
            "segment": item["segment"],
        }
    return answers


def reconciled(
    call: CallText,
    score: dict[str, object] | None,
    escalations: dict[str, object] | None,
) -> dict[str, object] | None:
    """The score part once the escalations part agrees with it (D-75): each
    check an escalation answers set no (escalated_checks), and the
    professionalism mark, the total and the band marked again in code. The
    score as it was with either part null, or nothing to set."""
    if score is None or escalations is None:
        return score
    # escalations_part's and score_call's own parts, built in this process.
    items = cast(list[dict[str, object]], escalations["items"])
    answers = escalated_checks(call, items)
    if not answers:
        return score
    components = dict(cast(dict[str, dict[str, object]], score["components"]))
    kept = components[PROFESSIONALISM.name]
    shown = {**cast(dict[str, dict[str, object]], kept["checks"]), **answers}
    components[PROFESSIONALISM.name] = {**kept, **_marked(PROFESSIONALISM, shown)}
    return {**score, **_totalled(components), "components": components}
