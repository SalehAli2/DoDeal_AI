"""unit_b.escalations (wave 2): the issues a manager must see -- the five the
BRD names and possible_broker -- each with the quote that shows it, merged
with the escalations stage 1 found in code (off_channel_contact, from a number
or an alarm phrase).

OVER-PROMISES ARE DECIDED IN CODE, NOT BY THE MODEL (D-104). Asked "is this an
over-promise?", a model sampled at its default temperature answered a
borderline line yes one time in three, and the call's score moved a band with
it. So the model now records facts only: every CLAIM the agent made about what
a price, rent, return, approval or handover will do (escalations_v5), what it
is about, and whether it was said as certain or hedged. Code decides: a
verified claim in the agent's own words, said as certain -- by the model's
label, or by a certainty word in the quote itself (CERTAIN_WORDS, with no
negation in the three words before it) -- is an over_promise_or_guarantee
escalation, at most MAX_PROMISE_FLAGS of them, source "claim". The same
claims decide the score's
no_over_promise (score.reconciled): one judge, not two. A model flag still
naming over_promise_or_guarantee (the v4 habit) is ignored. Every verified
claim, hedged ones too, is delivered in the part's claims list for a manager
to read.

  over_promise_or_guarantee       an agent's promise nobody can keep; from
                                  the claims, in code (above)
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

Every flag's and claim's quote goes through the quote check; a claim and the
agent's issues must come from an agent segment, possible_broker from a client
segment. A flag or claim without a real quote, or from the wrong side, is
dropped on its own, one by one (A5): the rest are kept and nothing is
reprompted. Then the claims past MAX_CLAIMS, the price flags
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
    quote_words,
    relocated,
    turns_around,
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

# The most over-promises raised from claims, in the order said: one is
# enough for the manager and the score; more floods the list. Lower drops a
# second real promise; higher repeats one promise said in three ways.
MAX_PROMISE_FLAGS = 3
# The most claims kept in the part, in order: more than any honest call makes.
MAX_CLAIMS = 10
# Words that make a claim certain whatever the model's label said (D-104),
# matched as the quote check reads words (evidence.quote_words); a phrase is
# its words in order. "100%" is left to the label: its sign is not a word, so
# it would match every hundred.
CERTAIN_WORDS: tuple[str, ...] = (
    "على طول",
    "دايما",
    "دائما",
    "أكيد",
    "بالتأكيد",
    "مضمون",
    "مضمونة",
    "always",
    "guaranteed",
    "guarantee",
    "definitely",
    "certainly",
    "for sure",
)
# How many words before a certainty word are read for a word that turns it
# around (evidence.turns_around): "مش أكيد" and "it is not always" stay as
# the model labelled them. Lower lets "not really always" through; higher
# lets an earlier "no" in the sentence cancel a real promise.
CERTAIN_NEGATION_WINDOW = 3
CERTAIN = "certain"
CLAIM_SOURCE = "claim"

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

# The issues escalations_v5 names, exactly.
type Issue = Literal[
    "wrong_price_or_terms",
    "rudeness_or_pressure",
    "unprofessional_competitor_talk",
    "qualified_no_next_step",
    "possible_broker",
]
# The issue escalations_v4 named and v5 does not (D-104): a model that still
# answers it out of habit is taken by the schema, so it costs no reprompt,
# and the flag is ignored (kept_flags). Refusing it would pay a second call
# for a flag nothing reads.
type RetiredIssue = Literal["over_promise_or_guarantee"]


# Who must have said the quote an issue rests on; any side for the rest.
_SPEAKER = {
    **dict.fromkeys(AGENT_ISSUES, AGENT),
    **dict.fromkeys(CLIENT_ISSUES, CLIENT),
}


class Flag(Strict):
    """One issue, and the quote that shows it; one with no quote is dropped
    (kept_flags), never refused by the schema."""

    issue: Issue | RetiredIssue
    quote: Quote
    segment: SegmentId


class Claim(Strict):
    """One statement of the agent's about the future of a property's value:
    what it is about, how it was said, and the quote that shows it."""

    about: Literal["price", "rent", "return", "approval", "handover", "other"]
    said_as: Literal["certain", "hedged"]
    quote: Quote
    segment: SegmentId


class Flags(Strict):
    """unit_b.escalations' answer, exactly: the agent's claims and any number
    of flags, both capped in code (kept_claims, kept_flags)."""

    claims: list[Claim]
    escalations: list[Flag]


def kept_flags(call: CallText, answer: Flags) -> Flags:
    """The answer with each flag whose quote fails the quote check -- or comes
    from the wrong side -- dropped on its own, then the price flags past
    MAX_PRICE_FLAGS and every flag past MAX_FLAGS, in the model's order."""
    kept: list[Flag] = []
    prices = 0
    for n, flag in enumerate(answer.escalations):
        if flag.issue == OVER_PROMISE:
            continue  # decided from the claims in code (D-104)
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


_CERTAIN = tuple(quote_words(phrase) for phrase in CERTAIN_WORDS)


def said_certain(claim: Claim) -> bool:
    """Whether a claim was said as certain (D-104): the model's label, or a
    CERTAIN_WORDS phrase in its quote with no word that turns it around in
    the CERTAIN_NEGATION_WINDOW words before it."""
    if claim.said_as == CERTAIN:
        return True
    said = quote_words(claim.quote or "")
    for phrase in _CERTAIN:
        for at in range(len(said) - len(phrase) + 1):
            if said[at : at + len(phrase)] != phrase:
                continue
            before = said[max(0, at - CERTAIN_NEGATION_WINDOW) : at]
            if not any(turns_around(word) for word in before):
                return True
    return False


def kept_claims(call: CallText, answer: Flags) -> list[Claim]:
    """The claims whose quote holds in an agent segment, in order, at most
    MAX_CLAIMS; the rest dropped one by one, never a reprompt."""
    kept = [
        claim
        for n, claim in enumerate(answer.claims)
        if not evidence_errors(
            call, f"claims.{n}", claim.quote, claim.segment, speaker=AGENT
        )
    ]
    return kept[:MAX_CLAIMS]


def promises(call: CallText, answer: Flags) -> list[Claim]:
    """The over-promises code finds (D-104): the kept claims said as certain,
    in order, at most MAX_PROMISE_FLAGS."""
    return [claim for claim in kept_claims(call, answer) if said_certain(claim)][
        :MAX_PROMISE_FLAGS
    ]


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
    kept = kept_flags(call, answer)
    return relocated(
        call, kept.model_copy(update={"claims": kept_claims(call, answer)})
    ), response


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


def _said(call: CallText, claim: Claim) -> dict[str, object]:
    """A kept claim as delivered: who and when from its segment, and whether
    code holds it certain."""
    index = None if claim.segment is None else call.index_of(claim.segment)
    assert index is not None  # kept_claims kept only claims found in a segment
    segment = call.segments[index]
    return {
        "about": claim.about,
        "said_as": CERTAIN if said_certain(claim) else claim.said_as,
        "speaker": role_of(segment),
        "start_s": segment.start_s,
        "segment": claim.segment,
        "quote": claim.quote,
    }


def _promise(call: CallText, claim: Claim) -> dict[str, object]:
    """A certain claim as an over_promise_or_guarantee escalation."""
    said = _said(call, claim)
    return {
        "type": OVER_PROMISE,
        "issue": OVER_PROMISE,
        "source": CLAIM_SOURCE,
        "about": claim.about,
        **{key: said[key] for key in ("speaker", "start_s", "segment", "quote")},
    }


def escalations_part(
    call: CallText, answer: Flags, stage1: Sequence[dict[str, object]]
) -> dict[str, object]:
    """Stage 2's escalations part: stage 1's, the model's as kept_flags keeps
    them and the over-promises code finds in the claims, by time; and every
    kept claim, in order (D-104)."""
    flags = kept_flags(call, answer).escalations
    items = [
        *stage1,
        *(_escalation(call, flag) for flag in flags),
        *(_promise(call, claim) for claim in promises(call, answer)),
    ]
    items.sort(key=lambda item: float(str(item["start_s"])))
    return {
        "items": items,
        "claims": [_said(call, claim) for claim in kept_claims(call, answer)],
    }
