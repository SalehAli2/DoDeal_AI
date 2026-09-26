"""possible_broker (escalations.py): a client who presents as a buyer but
talks like a broker, quoted from the client's own words, delivered as a
manager escalation beside the others."""

from __future__ import annotations

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.escalations import (
    POSSIBLE_BROKER,
    Flags,
    check_flags,
    escalations_part,
    find_flags,
)
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from tests.helpers.fake_llm import FakeLLM, json_response

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")


def _say(start: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


def _call(*segments: Segment) -> CallText:
    transcript = Transcript.of(segments, provider="fake", model="fake")
    return CallText.of(transcript, country_code="971")


# An invented call: a "buyer" who speaks for a client and asks about commission.
BROKER = _call(
    _say(0, "agent", "Good morning, you asked about the marina towers?"),
    _say(5, "lead", "Yes, my client is interested in two units there."),
    _say(10, "agent", "Great, they are ready and the price is fixed."),
    _say(15, "lead", "And what commission do you share with agents?"),
)
# An invented call: a genuine investor buying for themselves.
INVESTOR = _call(
    _say(0, "agent", "Good morning, you asked about the marina towers?"),
    _say(5, "lead", "Yes, I invest for my family, I want two units to rent out."),
    _say(10, "agent", "The rental yield there is about seven percent."),
)


def _flag(quote: str, segment: str, issue: str = POSSIBLE_BROKER) -> dict:
    return {"issue": issue, "quote": quote, "segment": segment}


async def _found(call: CallText, flags: list[dict]) -> dict:
    llm = FakeLLM(json_response({"escalations": flags}))
    answer, _ = await find_flags(llm, call, scope=SCOPE, settings=get_settings())
    assert llm.call_count == 1
    return escalations_part(call, answer, [])


# --- the guards --------------------------------------------------------------------


async def test_my_client_is_interested_raises_possible_broker() -> None:
    """The guard: the client's own "my client is interested" and commission
    question go out as possible_broker escalations, beside the others."""
    part = await _found(
        BROKER,
        [
            _flag("my client is interested", "s2"),
            _flag("what commission do you share with agents", "s4"),
        ],
    )
    assert part["items"] == [
        {
            "type": POSSIBLE_BROKER,
            "issue": POSSIBLE_BROKER,
            "source": "model",
            "speaker": "client",
            "start_s": 5.0,
            "segment": "s2",
            "quote": "my client is interested",
        },
        {
            "type": POSSIBLE_BROKER,
            "issue": POSSIBLE_BROKER,
            "source": "model",
            "speaker": "client",
            "start_s": 15.0,
            "segment": "s4",
            "quote": "what commission do you share with agents",
        },
    ]


async def test_a_genuine_investor_raises_nothing() -> None:
    """The guard: several units for the client's own family is an investor;
    nothing is flagged and nothing goes out."""
    assert await _found(INVESTOR, []) == {"items": []}


# --- the rules ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "flag", "error"),
    [
        (BROKER, _flag("the price is fixed", "s3"), "quote_wrong_speaker"),
        (INVESTOR, _flag("my client is interested", "s2"), "quote_not_in_segment"),
    ],
    ids=["the-agents-words", "words-the-investor-never-said"],
)
def test_possible_broker_rests_on_the_clients_own_words(
    call: CallText, flag: dict, error: str
) -> None:
    with pytest.raises(OutputValidationError) as refused:
        check_flags(call)(Flags.model_validate({"escalations": [flag]}))
    assert refused.value.errors == (("escalations.0", error),)
