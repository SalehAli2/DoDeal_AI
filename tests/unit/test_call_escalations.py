"""unit_b.escalations (escalations.py): the five BRD issues, each with a real
quote -- the agent's own for the four only an agent commits -- merged by time
with stage 1's off_channel_contact, a price claim going out to be verified."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.jobs import JobStatus, Stage2State, create_job, read_job, transition
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_ESCALATIONS
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.escalations import (
    MAX_FLAGS,
    MAX_PRICE_FLAGS,
    Flags,
    escalations_part,
    find_flags,
    kept_flags,
)
from dodeal_ai.units.call_intelligence.evidence import CallText, evidence_errors
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    ESCALATIONS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import ESCALATIONS, wave2
from tests.helpers.fake_llm import FakeLLM, json_response

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


def _say(start: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


# An invented call: a guarantee, a price, and a qualified client left hanging.
SEGMENTS = (
    _say(0, "agent", "This unit will double in value in two years, guaranteed."),
    _say(5, "lead", "My budget is two million and I want to buy this year."),
    _say(10, "agent", "The price is 1,500,000 AED with no service charges."),
    _say(15, "lead", "Alright, thank you, goodbye."),
)
TRANSCRIPT = Transcript.of(SEGMENTS, provider="fake", model="fake")
OFF_CHANNEL = {
    "type": "off_channel_contact",
    "source": "alarm_phrase",
    "phrase": 0,
    "speaker": "agent",
    "start_s": 12.0,
    "segment": "s3",
}


def _call() -> CallText:
    return CallText.of(TRANSCRIPT, country_code="971")


def _flag(issue: str, quote: str, segment: str) -> dict[str, str]:
    return {"issue": issue, "quote": quote, "segment": segment}


GUARANTEE = _flag("over_promise_or_guarantee", "will double in value", "s1")
PRICE = _flag("wrong_price_or_terms", "The price is 1,500,000 AED", "s3")
HANGING = _flag("qualified_no_next_step", "My budget is two million", "s2")
ANSWER = {"escalations": [PRICE, GUARANTEE, HANGING]}


def _kept(*flags: dict[str, str | None]) -> list[dict[str, str | None]]:
    """The flags kept_flags keeps, in order."""
    answer = kept_flags(_call(), Flags.model_validate({"escalations": list(flags)}))
    return [flag.model_dump() for flag in answer.escalations]


# --- the guard ---------------------------------------------------------------------


async def test_one_bad_flag_is_dropped_and_the_rest_kept() -> None:
    """The guard (A5): a flag without a real quote is dropped on its own; the
    pass is kept with the others, on one call, and nothing is reprompted."""
    invented = _flag("rudeness_or_pressure", "sign today or lose it", "s3")
    llm = FakeLLM(json_response({"escalations": [PRICE, invented, GUARANTEE]}))

    answer, _ = await find_flags(llm, _call(), scope=SCOPE, settings=get_settings())
    assert llm.call_count == 1
    assert [flag.model_dump() for flag in answer.escalations] == [PRICE, GUARANTEE]
    assert _kept(invented) == []
    assert _kept({**HANGING, "quote": None, "segment": None}, HANGING) == [HANGING]


def test_flags_over_the_caps_are_dropped_by_order_never_refused() -> None:
    """A5: past MAX_FLAGS, and past MAX_PRICE_FLAGS for price claims, flags
    are dropped in the model's order; the schema takes any number."""
    prices = [
        _flag("wrong_price_or_terms", quote, "s3")
        for quote in ("The price is", "1,500,000 AED", "no service charges", "price is 1,500,000")
    ]  # fmt: skip
    assert (MAX_PRICE_FLAGS, MAX_FLAGS) == (3, 10)
    assert _kept(*prices, GUARANTEE) == [*prices[:3], GUARANTEE]
    many = [GUARANTEE] * 12
    assert _kept(*many) == [GUARANTEE] * 10


@pytest.mark.parametrize(
    ("flag", "error"),
    [
        (
            _flag("unprofessional_competitor_talk", "My budget is two million", "s2"),
            "quote_wrong_speaker",
        ),
        (_flag("over_promise_or_guarantee", "guaranteed", "s9"), "segment_unknown"),
    ],
    ids=["agent-issue-from-the-client", "segment-unknown"],
)
def test_every_flag_is_quote_checked_and_an_agent_issue_is_the_agents(
    flag: dict[str, str], error: str
) -> None:
    assert evidence_errors(_call(), "x", flag["quote"], flag["segment"], speaker="agent") == [
        ("x", error)
    ]  # fmt: skip
    assert _kept(flag) == []


def test_a_qualified_client_with_no_next_step_may_quote_the_client() -> None:
    assert _kept(HANGING) == [HANGING]


def test_the_prompt_tells_a_promise_from_a_price() -> None:
    from dodeal_ai.core.prompting import build_prompt

    text = " ".join(build_prompt(ESCALATIONS_TEMPLATE, caller_data="").stable.split())
    assert "what a price, rent or value WILL do" in text
    assert "what a price or term IS" in text


# --- the part --------------------------------------------------------------------------


def test_stage1s_and_the_models_escalations_merge_by_time() -> None:
    part = escalations_part(_call(), Flags.model_validate(ANSWER), [OFF_CHANNEL])
    assert [(e["type"], e["segment"]) for e in part["items"]] == [
        ("over_promise_or_guarantee", "s1"),
        ("qualified_no_next_step", "s2"),
        ("claim_to_verify", "s3"),
        ("off_channel_contact", "s3"),
    ]
    claim = part["items"][2]
    assert claim == {
        "type": "claim_to_verify",
        "issue": "wrong_price_or_terms",
        "source": "model",
        "speaker": "agent",
        "start_s": 10.0,
        "segment": "s3",
        "quote": "The price is 1,500,000 AED",
    }
    assert part["items"][1]["speaker"] == "client"


def test_nothing_flagged_keeps_stage1s_alone() -> None:
    part = escalations_part(_call(), Flags(escalations=[]), [OFF_CHANNEL])
    assert part == {"items": [OFF_CHANNEL]}


@pytest.mark.parametrize("issue", ["price_error", "claim_to_verify"])
def test_an_issue_off_the_list_is_refused_by_the_schema(issue: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Flags.model_validate({"escalations": [{**PRICE, "issue": issue}]})


# --- in wave 2 ---------------------------------------------------------------------------


async def _wave2(escalations: list[Any]) -> tuple[FakeLLM, Any]:
    await create_job(
        "tenant-a",
        7,
        job_id="job-1",
        request_id="req-1",
        queue="arq:calls:normal",
        metadata={"author_id": 27, "duration_seconds": 150},
        now=NOW,
    )
    job = await read_job("tenant-a", "job-1")
    assert job is not None
    await transition(
        job, JobStatus.DONE, now=NOW, ttl_seconds=600, stage2=Stage2State.PENDING
    )
    llm = FakeLLM()
    llm.script_for(OBJECTIONS_TEMPLATE, json_response({"objections": []}))
    llm.script_for(ESCALATIONS_TEMPLATE, *escalations)
    wave = await wave2(
        llm,
        await read_job("tenant-a", "job-1"),
        CallsConfig(),
        TRANSCRIPT,
        work={},
        scope=SCOPE,
        settings=get_settings(),
        usage=PassUsage(),
        eligible=True,
        stage1_escalations=[OFF_CHANNEL],
    )
    return llm, wave


async def test_wave2_merges_the_pass_into_the_escalations_part() -> None:
    llm, wave = await _wave2([json_response(ANSWER)])
    part = wave.parts[ESCALATIONS]
    assert part is not None and len(part["items"]) == 4
    (sent,) = [c for c in llm.calls if c.profile == PROFILE_UNIT_B_ESCALATIONS]
    assert sent.max_output_tokens == 2500


async def test_a_failed_escalations_pass_is_null_with_its_reason() -> None:
    _, wave = await _wave2([json_response({}), json_response({})])
    assert (wave.parts[ESCALATIONS], wave.reasons[ESCALATIONS]) == (
        None,
        "escalations_malformed_output",
    )
