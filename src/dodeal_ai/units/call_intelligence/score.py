"""unit_b.score (wave 2) and call_rubric_v1: the model answers yes-or-no checks
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

A SCORE ONLY when the tenant's scoring_enabled is on, the call is eligible,
the client engaged (a talk share of at least 0.20), and the objections pass
answered; otherwise null, with the reason: scoring_off, not_eligible,
not_engaged or objections_unavailable -- and the pass is not run.

THE EVIDENCE: each check's quote goes through the quote check; one is owed
wherever the answer says something was said -- every yes, but for
no_over_promise and no_pressure, where it is every no.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_SCORE, task_ceiling
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Errors,
    Quote,
    SegmentId,
    Strict,
    evidence_errors,
    quote_errors,
)
from dodeal_ai.units.call_intelligence.prompts import (
    REPROMPT_TAIL_TEMPLATE,
    SCORE_TEMPLATE,
    build_call_prompt,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

SCORE_LABEL = "llm.unit_b.score"
RUBRIC_VERSION = "call_rubric_v1"

# What the answer may cost (register item 15): thirteen checks with a quote
# each, sized against an Arabic call on a non-reasoning model.
SCORE_MAX_OUTPUT_TOKENS = 2500
# The same answer on a reasoning profile, whose hidden reasoning is spent
# inside the ceiling (register item 116): the ceiling follows the profile.
SCORE_REASONING_MAX_OUTPUT_TOKENS = 6000

# The client's talk share at which the agent listened more, and below which
# the client did not engage enough to judge the call.
LISTENED_MORE_SHARE = 0.40
ENGAGED_SHARE = 0.20
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
    return await call_model(
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
        ),
        check=check_score(call),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
    )


def round_half_up(value: Fraction) -> int:
    """The nearest whole number, a half always up."""
    return math.floor(value + Fraction(1, 2))


def mark(weight: int, passed: int, of: int) -> int:
    """`weight` split evenly over `of` checks, times those passed."""
    return round_half_up(Fraction(weight * passed, of))


def band_of(total: int) -> str:
    """The band a total falls in."""
    return next(name for floor, name in BANDS if total >= floor)


def score_gate(
    *,
    scoring_enabled: bool,
    eligible: bool,
    client_share: float | None,
    objections_answered: bool,
) -> str | None:
    """Why the call gets no score, or None when it gets one."""
    if not scoring_enabled:
        return SCORING_OFF
    if not eligible:
        return NOT_ELIGIBLE
    if client_share is None or client_share < ENGAGED_SHARE:
        return NOT_ENGAGED
    if not objections_answered:
        return OBJECTIONS_UNAVAILABLE
    return None


def _decided(answer: str) -> dict[str, object]:
    """A check decided in code, beside the model's."""
    return {"answer": answer, "source": "code"}


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
    passed = sum(1 for check in shown.values() if check["answer"] == YES)
    return {
        "weight": component.weight,
        "mark": mark(component.weight, passed, len(component.checks)),
        "checks": shown,
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
    marked = [c for c in components.values() if c["mark"] is not None]
    applicable = sum(int(str(c["weight"])) for c in marked)
    raw = sum(int(str(c["mark"])) for c in marked)
    total = round_half_up(Fraction(raw * 100, applicable))
    return {
        "total": total,
        "band": band_of(total),
        "raw": raw,
        "applicable_weight": applicable,
        "product_question_asked": checks.product_question_asked.model_dump(mode="json"),
        "components": components,
    }
