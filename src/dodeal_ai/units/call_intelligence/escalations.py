"""unit_b.escalations (wave 2): the issues a manager must see -- the five the
BRD names and possible_broker -- each with the quote that shows it, merged
with the escalations stage 1 found in code (off_channel_contact, from a number
or an alarm phrase).

  over_promise_or_guarantee       an agent's promise nobody can keep
  wrong_price_or_terms            an agent's price or terms; goes out as
                                  claim_to_verify until a project feed can
                                  say whether it was wrong
  rudeness_or_pressure            an agent rude to the client, or pushing
  unprofessional_competitor_talk  an agent running down a competitor
  qualified_no_next_step          a qualified client left with no next step
  possible_broker                 a client who presents as a buyer but talks
                                  like a broker: asks about commission, says
                                  "my client(s)", asks for several units for
                                  others; one flag per quote

Every flag's quote goes through the quote check; the first four must come
from an agent segment and possible_broker from a client segment. A flag
without a real quote, or from the wrong side, is dropped on its own, one by
one (A5): the rest are kept and nothing is reprompted. Then the price flags
past MAX_PRICE_FLAGS and every flag past MAX_FLAGS are dropped, in the model's
order, never a schema error. Only a broken shape is malformed: reprompted
once, then the pass fails and the escalations part is null. Who said it and
when are read from the found segment in code, never from the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_ESCALATIONS, task_ceiling
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Quote,
    SegmentId,
    Strict,
    evidence_errors,
    relocated,
)
from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    CLIENT,
    ESCALATIONS_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    build_call_prompt,
    role_of,
)
from dodeal_ai.units.structured_intelligence.llm_call import call_model

ESCALATIONS_LABEL = "llm.unit_b.escalations"

# What the answer may cost (register item 15): up to ten flags with a quote
# each, sized against an Arabic call on a non-reasoning model.
ESCALATIONS_MAX_OUTPUT_TOKENS = 2500
# The same answer on a reasoning profile, whose hidden reasoning is spent
# inside the ceiling (register item 116): the ceiling follows the profile.
ESCALATIONS_REASONING_MAX_OUTPUT_TOKENS = 6000

# The most flags kept, in the model's order: more than any honest call
# raises. Higher lets a model flood the manager; lower drops real ones.
MAX_FLAGS = 10
# The most price or terms claims kept, in order: each is checked by hand, and
# a call rarely states more than three prices. Higher floods that check;
# lower drops a real claim.
MAX_PRICE_FLAGS = 3

WRONG_PRICE = "wrong_price_or_terms"
CLAIM_TO_VERIFY = "claim_to_verify"
# An agent's promise nobody can keep: it also answers the score's
# no_over_promise (score.py, D-75).
OVER_PROMISE = "over_promise_or_guarantee"

# The issues only the agent can commit, so only an agent segment can show.
AGENT_ISSUES = frozenset(
    {
        OVER_PROMISE,
        WRONG_PRICE,
        "rudeness_or_pressure",
        "unprofessional_competitor_talk",
    }
)

# The issue only the client can show: a buyer who talks like a broker.
POSSIBLE_BROKER = "possible_broker"
CLIENT_ISSUES = frozenset({POSSIBLE_BROKER})

type Issue = Literal[
    "over_promise_or_guarantee",
    "wrong_price_or_terms",
    "rudeness_or_pressure",
    "unprofessional_competitor_talk",
    "qualified_no_next_step",
    "possible_broker",
]


# Who must have said the quote an issue rests on; any side for the rest.
_SPEAKER = {
    **dict.fromkeys(AGENT_ISSUES, AGENT),
    **dict.fromkeys(CLIENT_ISSUES, CLIENT),
}


class Flag(Strict):
    """One issue, and the quote that shows it; one with no quote is dropped
    (kept_flags), never refused by the schema."""

    issue: Issue
    quote: Quote
    segment: SegmentId


class Flags(Strict):
    """unit_b.escalations' answer, exactly: any number of flags, capped in
    code (kept_flags)."""

    escalations: list[Flag]


def kept_flags(call: CallText, answer: Flags) -> Flags:
    """The answer with each flag whose quote fails the quote check -- or comes
    from the wrong side -- dropped on its own, then the price flags past
    MAX_PRICE_FLAGS and every flag past MAX_FLAGS, in the model's order."""
    kept: list[Flag] = []
    prices = 0
    for n, flag in enumerate(answer.escalations):
        speaker = _SPEAKER.get(flag.issue)
        where = f"escalations.{n}"
        if evidence_errors(call, where, flag.quote, flag.segment, speaker=speaker):
            continue
        if flag.issue == WRONG_PRICE:
            prices += 1
            if prices > MAX_PRICE_FLAGS:
                continue
        if len(kept) < MAX_FLAGS:
            kept.append(flag)
    return answer.model_copy(update={"escalations": kept})


async def find_flags(
    client: LLMClient, call: CallText, *, scope: TenantScope, settings: Settings
) -> tuple[Flags, LLMResponse]:
    """unit_b.escalations: one call, or two when the first answer's shape is
    broken; its flags as kept_flags keeps them."""
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
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=REPROMPT_TAILS,
    )
    return relocated(call, kept_flags(call, answer)), response


def _escalation(call: CallText, flag: Flag) -> dict[str, object]:
    """A flag as an escalation, beside stage 1's: who and when from the
    segment, and a price or terms claim as one to verify."""
    index = None if flag.segment is None else call.index_of(flag.segment)
    assert index is not None  # kept_flags kept only flags found in a segment
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
    """Stage 2's escalations part: stage 1's and the model's as kept_flags
    keeps them, by time."""
    flags = kept_flags(call, answer).escalations
    items = [*stage1, *(_escalation(call, flag) for flag in flags)]
    items.sort(key=lambda item: float(str(item["start_s"])))
    return {"items": items}
