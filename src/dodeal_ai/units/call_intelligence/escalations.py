"""unit_b.escalations (wave 2): the five BRD issues a manager must see, each
with the quote that shows it, merged with the escalations stage 1 found in
code (off_channel_contact, from a number or an alarm phrase).

  over_promise_or_guarantee       an agent's promise nobody can keep
  wrong_price_or_terms            an agent's price or terms; goes out as
                                  claim_to_verify until a project feed can
                                  say whether it was wrong
  rudeness_or_pressure            an agent rude to the client, or pushing
  unprofessional_competitor_talk  an agent running down a competitor
  qualified_no_next_step          a qualified client left with no next step

Every flag's quote goes through the quote check, and the first four must come
from an agent segment: a flag without a real quote is a malformed answer,
reprompted once, then the pass fails and the escalations part is null. Who
said it and when are read from the cited segment in code, never from the model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Literal

from pydantic import Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_ESCALATIONS, task_ceiling
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Cited,
    Errors,
    Said,
    Strict,
    evidence_errors,
    relocated,
)
from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    ESCALATIONS_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    build_call_prompt,
    role_of,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

ESCALATIONS_LABEL = "llm.unit_b.escalations"

# What the answer may cost (register item 15): up to ten flags with a quote
# each, sized against an Arabic call on a non-reasoning model.
ESCALATIONS_MAX_OUTPUT_TOKENS = 2500
# The same answer on a reasoning profile, whose hidden reasoning is spent
# inside the ceiling (register item 116): the ceiling follows the profile.
ESCALATIONS_REASONING_MAX_OUTPUT_TOKENS = 6000

# More flags than any honest call raises.
MAX_FLAGS = 10

WRONG_PRICE = "wrong_price_or_terms"
CLAIM_TO_VERIFY = "claim_to_verify"

# The issues only the agent can commit, so only an agent segment can show.
AGENT_ISSUES = frozenset(
    {
        "over_promise_or_guarantee",
        WRONG_PRICE,
        "rudeness_or_pressure",
        "unprofessional_competitor_talk",
    }
)

type Issue = Literal[
    "over_promise_or_guarantee",
    "wrong_price_or_terms",
    "rudeness_or_pressure",
    "unprofessional_competitor_talk",
    "qualified_no_next_step",
]


class Flag(Strict):
    """One issue, and the quote that shows it."""

    issue: Issue
    quote: Said
    segment: Cited


class Flags(Strict):
    """unit_b.escalations' answer, exactly."""

    escalations: Annotated[list[Flag], Field(max_length=MAX_FLAGS)]


def check_flags(call: CallText) -> Callable[[Flags], None]:
    """Every flag's quote real, and the agent's issues from the agent."""

    def check(answer: Flags) -> None:
        errors: Errors = []
        for n, flag in enumerate(answer.escalations):
            speaker = AGENT if flag.issue in AGENT_ISSUES else None
            errors += evidence_errors(
                call, f"escalations.{n}", flag.quote, flag.segment, speaker=speaker
            )
        if errors:
            raise output_rejected(ESCALATIONS_LABEL, tuple(errors))

    return check


async def find_flags(
    client: LLMClient, call: CallText, *, scope: TenantScope, settings: Settings
) -> tuple[Flags, LLMResponse]:
    """unit_b.escalations: one call, or two when the first answer is malformed."""
    answer, response = await call_model(
        client,
        build_call_prompt(ESCALATIONS_TEMPLATE, call.data()),
        Flags,
        ESCALATIONS_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_B_ESCALATIONS,
        max_output_tokens=task_ceiling(
            settings,
            PROFILE_UNIT_B_ESCALATIONS,
            plain=ESCALATIONS_MAX_OUTPUT_TOKENS,
            reasoning=ESCALATIONS_REASONING_MAX_OUTPUT_TOKENS,
            client=client,
        ),
        check=check_flags(call),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=REPROMPT_TAILS,
    )
    return relocated(call, answer), response


def _escalation(call: CallText, flag: Flag) -> dict[str, object]:
    """A flag as an escalation, beside stage 1's: who and when from the
    segment, and a price or terms claim as one to verify."""
    index = call.index_of(flag.segment)
    assert index is not None  # the quote check found the segment
    segment = call.segments[index]
    return {
        "type": CLAIM_TO_VERIFY if flag.issue == WRONG_PRICE else flag.issue,
        "issue": flag.issue,
        "source": "model",
        "speaker": role_of(segment),
        "start_s": segment.start_s,
        "segment": flag.segment,
        "quote": flag.quote,
    }


def escalations_part(
    call: CallText, answer: Flags, stage1: Sequence[dict[str, object]]
) -> dict[str, object]:
    """Stage 2's escalations part: stage 1's and the model's, by time."""
    items = [*stage1, *(_escalation(call, flag) for flag in answer.escalations)]
    items.sort(key=lambda item: float(str(item["start_s"])))
    return {"items": items}
