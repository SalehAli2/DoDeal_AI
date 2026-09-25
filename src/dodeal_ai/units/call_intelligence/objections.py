"""unit_b.objections (wave 2): the client's objections on a call, each in one of
the nine BRD categories (objection_list_v1), with the evidence for it.

WHAT EACH ONE CARRIES, every quote under the quote check (evidence.py):

  category    one of OBJECTION_CATEGORIES, and nothing else
  quote       where the CLIENT raised it -- a client segment
  addressed   yes, with the AGENT's quote from an agent segment; or no
  satisfied   yes or no, with the CLIENT's quote showing it; or unclear

A missing, invented or wrong-speaker quote is a malformed answer: the pass is
reprompted once, then fails, and the objections part of stage 2 is null. The
score's objections component is computed from these in code (score.py).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_OBJECTIONS, task_ceiling
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Cited,
    Errors,
    Quote,
    Said,
    SegmentId,
    Strict,
    evidence_errors,
    quote_errors,
)
from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    CLIENT,
    OBJECTIONS_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    build_call_prompt,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

OBJECTIONS_LABEL = "llm.unit_b.objections"

# The nine categories the BRD names, in its order. The list is versioned: a
# change is a new version, and stage 2 carries the one it ran on.
OBJECTION_LIST_VERSION = "objection_list_v1"
OBJECTION_CATEGORIES = (
    "price",
    "timing",
    "competitor",
    "trust",
    "property_fit",
    "payment_finance",
    "location",
    "third_party_approval",
    "service_charges_fees",
)

# What the answer may cost (register item 15): up to ten objections, each with
# three quotes, sized against an Arabic call on a non-reasoning model.
OBJECTIONS_MAX_OUTPUT_TOKENS = 2500
# The same answer on a reasoning profile, whose hidden reasoning is spent
# inside the ceiling (register item 116): the ceiling follows the profile.
OBJECTIONS_REASONING_MAX_OUTPUT_TOKENS = 6000

# More than any honest call raises: the same worry again is the same objection.
MAX_OBJECTIONS = 10

YES = "yes"

type Category = Literal[
    "price",
    "timing",
    "competitor",
    "trust",
    "property_fit",
    "payment_finance",
    "location",
    "third_party_approval",
    "service_charges_fees",
]


class Objection(Strict):
    """One objection the client raised, and what came of it."""

    category: Category
    quote: Said
    segment: Cited
    addressed: Literal["yes", "no"]
    agent_quote: Quote
    agent_segment: SegmentId
    satisfied: Literal["yes", "no", "unclear"]
    satisfied_quote: Quote
    satisfied_segment: SegmentId


class Objections(Strict):
    """unit_b.objections' answer, exactly."""

    objections: Annotated[list[Objection], Field(max_length=MAX_OBJECTIONS)]


def _objection_errors(call: CallText, where: str, found: Objection) -> Errors:
    """Each quote where it must be, from the one who must have said it."""
    errors = evidence_errors(call, where, found.quote, found.segment, speaker=CLIENT)
    answered = evidence_errors if found.addressed == YES else quote_errors
    errors += answered(
        call, f"{where}.agent", found.agent_quote, found.agent_segment, speaker=AGENT
    )
    shown = quote_errors if found.satisfied == "unclear" else evidence_errors
    errors += shown(
        call,
        f"{where}.satisfied",
        found.satisfied_quote,
        found.satisfied_segment,
        speaker=CLIENT,
    )
    return errors


def check_objections(call: CallText) -> Callable[[Objections], None]:
    """The rules the schema cannot hold (module docstring), for call_model."""

    def check(answer: Objections) -> None:
        errors: Errors = []
        for n, found in enumerate(answer.objections):
            errors += _objection_errors(call, f"objections.{n}", found)
        if errors:
            raise output_rejected(OBJECTIONS_LABEL, tuple(errors))

    return check


async def find_objections(
    client: LLMClient, call: CallText, *, scope: TenantScope, settings: Settings
) -> tuple[Objections, LLMResponse]:
    """unit_b.objections: one call, or two when the first answer is malformed."""
    return await call_model(
        client,
        build_call_prompt(OBJECTIONS_TEMPLATE, call.data()),
        Objections,
        OBJECTIONS_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_B_OBJECTIONS,
        max_output_tokens=task_ceiling(
            settings,
            PROFILE_UNIT_B_OBJECTIONS,
            plain=OBJECTIONS_MAX_OUTPUT_TOKENS,
            reasoning=OBJECTIONS_REASONING_MAX_OUTPUT_TOKENS,
            client=client,
        ),
        check=check_objections(call),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=REPROMPT_TAILS,
    )


def objections_part(answer: Objections) -> dict[str, object]:
    """Stage 2's objections part: each objection, and the three counts the
    score reads -- raised, addressed and satisfied."""
    found = answer.objections
    return {
        "raised": len(found),
        "addressed": sum(1 for o in found if o.addressed == YES),
        "satisfied": sum(1 for o in found if o.satisfied == YES),
        "items": [o.model_dump(mode="json") for o in found],
    }
