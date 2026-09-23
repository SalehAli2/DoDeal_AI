"""unit_b.objections (objections.py) and wave 2 (wave2.py) on the FakeLLM: the
nine categories, every quote checked and from the right speaker, one reprompt,
the counts the score reads, and a failed pass leaving its part null."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any, get_args

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import MalformedOutputError
from dodeal_ai.core.jobs import (
    JobStatus,
    Stage2State,
    create_job,
    read_job,
    read_work,
    transition,
)
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_OBJECTIONS
from dodeal_ai.core.prompting import build_prompt
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence import objections
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.objections import (
    OBJECTION_CATEGORIES,
    Category,
    Objections,
    check_objections,
    find_objections,
    objections_part,
)
from dodeal_ai.units.call_intelligence.paid import JobGone, PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    ESCALATIONS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import OBJECTIONS, wave2
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


# An invented call: a price objection answered and accepted, a spouse to ask.
SEGMENTS = (
    _say(0, "agent", "Good morning, this is the sales office about the villa."),
    _say(5, "lead", "Honestly the price is too high for me, and the fees worry me."),
    _say(10, "agent", "We can offer a payment plan over four years with no fees."),
    _say(15, "lead", "That sounds fair, a payment plan works for me."),
    _say(20, "lead", "I still need my wife to agree before I sign anything."),
)
TRANSCRIPT = Transcript.of(SEGMENTS, provider="fake", model="fake")


def _call() -> CallText:
    return CallText.of(TRANSCRIPT, country_code="971")


PRICE: dict[str, Any] = {
    "category": "price",
    "quote": "the price is too high for me",
    "segment": "s2",
    "addressed": "yes",
    "agent_quote": "We can offer a payment plan over four years",
    "agent_segment": "s3",
    "satisfied": "yes",
    "satisfied_quote": "a payment plan works for me",
    "satisfied_segment": "s4",
}
SPOUSE: dict[str, Any] = {
    "category": "third_party_approval",
    "quote": "I still need my wife to agree",
    "segment": "s5",
    "addressed": "no",
    "agent_quote": None,
    "agent_segment": None,
    "satisfied": "unclear",
    "satisfied_quote": None,
    "satisfied_segment": None,
}
ANSWER = {"objections": [PRICE, SPOUSE]}


def _refused(*found: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    answer = Objections.model_validate({"objections": list(found)})
    with pytest.raises(OutputValidationError) as refused:
        check_objections(_call())(answer)
    return refused.value.errors


async def _find(llm: FakeLLM):
    return await find_objections(llm, _call(), scope=SCOPE, settings=get_settings())


# --- the guard ---------------------------------------------------------------------


async def test_an_invented_quote_fails_after_one_reprompt() -> None:
    invented = {**PRICE, "quote": "this villa is far too expensive"}
    answer = json_response({"objections": [invented]})
    llm = FakeLLM(answer, answer)

    with pytest.raises(MalformedOutputError):
        await _find(llm)

    assert llm.call_count == 2
    tail = build_prompt(REPROMPT_TAIL_TEMPLATE, caller_data="").stable
    assert (llm.prompts[0].tail, llm.prompts[1].tail) == ("", tail)
    assert llm.prompts[1].variable == llm.prompts[0].variable


# --- the rules ---------------------------------------------------------------------


async def test_a_true_answer_passes_and_is_counted() -> None:
    llm = FakeLLM(json_response(ANSWER))
    found, _ = await _find(llm)

    part = objections_part(found)
    assert (part["raised"], part["addressed"], part["satisfied"]) == (2, 1, 1)
    assert part["items"] == [PRICE, SPOUSE]
    (sent,) = llm.calls
    assert (sent.profile, sent.max_output_tokens) == (PROFILE_UNIT_B_OBJECTIONS, 2500)
    assert (
        sent.prompt.stable == build_prompt(OBJECTIONS_TEMPLATE, caller_data="").stable
    )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"segment": "s3"}, ("objections.0", "quote_not_in_segment")),
        (
            {"quote": "We can offer a payment plan", "segment": "s3"},
            ("objections.0", "quote_wrong_speaker"),
        ),
        (
            {"agent_quote": None, "agent_segment": None},
            ("objections.0.agent", "quote_missing"),
        ),
        (
            {"agent_quote": "a payment plan works for me", "agent_segment": "s4"},
            ("objections.0.agent", "quote_wrong_speaker"),
        ),
        (
            {"satisfied_quote": None, "satisfied_segment": None},
            ("objections.0.satisfied", "quote_missing"),
        ),
        (
            {
                "satisfied": "no",
                "satisfied_quote": "I will think",
                "satisfied_segment": "s4",
            },
            ("objections.0.satisfied", "quote_not_in_segment"),
        ),
    ],
    ids=[
        "raised-elsewhere",
        "raised-by-the-agent",
        "addressed-without-quote",
        "addressed-by-the-client",
        "satisfied-without-quote",
        "dissatisfied-invented",
    ],
)
def test_every_quote_is_checked_and_from_its_speaker(
    change: dict[str, Any], error: tuple[str, str]
) -> None:
    assert _refused({**copy.deepcopy(PRICE), **change}) == (error,)


def test_a_quote_given_where_none_is_owed_is_still_checked() -> None:
    stray = {**SPOUSE, "agent_quote": "nothing like it", "agent_segment": "s3"}
    assert _refused(stray) == (("objections.0.agent", "quote_not_in_segment"),)


@pytest.mark.parametrize(
    "change",
    [{"category": "parking"}, {"addressed": "maybe"}, {"satisfied": "happy"}],
)
def test_anything_off_the_lists_is_refused_by_the_schema(change: dict) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Objections.model_validate({"objections": [{**PRICE, **change}]})


def test_the_list_is_the_nine_brd_categories_and_versioned() -> None:
    assert get_args(Category.__value__) == OBJECTION_CATEGORIES
    assert len(OBJECTION_CATEGORIES) == 9
    assert objections.OBJECTION_LIST_VERSION == "objection_list_v1"


def test_no_objection_is_an_empty_part() -> None:
    assert objections_part(Objections(objections=[])) == {
        "raised": 0,
        "addressed": 0,
        "satisfied": 0,
        "items": [],
    }


# --- wave 2 ---------------------------------------------------------------------------


async def _done_job():
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
    job = await read_job("tenant-a", "job-1")
    assert job is not None
    return job


async def _wave2(llm: FakeLLM, job, work: dict | None = None, usage=None):
    llm.script_for(ESCALATIONS_TEMPLATE, json_response({"escalations": []}))
    return await wave2(
        llm,
        job,
        CallsConfig(),
        TRANSCRIPT,
        work=work or {},
        scope=SCOPE,
        settings=get_settings(),
        usage=usage or PassUsage(),
        eligible=True,
        stage1_escalations=[],
    )


async def test_wave2_runs_the_objections_pass_and_keeps_its_answer() -> None:
    job = await _done_job()
    usage = PassUsage()
    wave = await _wave2(FakeLLM(json_response(ANSWER)), job, usage=usage)

    assert wave.parts[OBJECTIONS] is not None
    assert wave.parts[OBJECTIONS]["raised"] == 2
    assert wave.reasons == {"score": "scoring_off"}
    assert wave.models[OBJECTIONS] == "fake-model-pinned"
    assert usage.tokens[OBJECTIONS]["calls"] == 1
    kept = await read_work("tenant-a", "job-1")
    assert kept[OBJECTIONS]["answer"] == ANSWER
    assert (await read_job("tenant-a", "job-1")).passes == {
        OBJECTIONS: 1,
        "escalations": 1,
    }


async def test_a_failed_pass_leaves_its_part_null_with_its_reason() -> None:
    job = await _done_job()
    wave = await _wave2(FakeLLM(json_response({}), json_response({})), job)
    assert (wave.parts[OBJECTIONS], wave.reasons[OBJECTIONS]) == (
        None,
        "objections_malformed_output",
    )


async def test_a_kept_answer_is_never_paid_for_again() -> None:
    job = await _done_job()
    work = {OBJECTIONS: {"answer": ANSWER, "model": "kept-model"}}
    llm = FakeLLM()
    wave = await _wave2(llm, job, work)
    assert [call.profile for call in llm.calls] == ["unit_b.escalations"]
    assert wave.models[OBJECTIONS] == "kept-model"


async def test_a_stage2_no_longer_pending_stops_before_any_call() -> None:
    job = await _done_job()
    await jobs_settle(job)
    llm = FakeLLM(json_response(ANSWER))
    with pytest.raises(JobGone):
        await _wave2(llm, job)
    assert llm.call_count == 0


async def jobs_settle(job) -> None:
    from dodeal_ai.core.jobs import settle_stage2

    await settle_stage2(job, Stage2State.FAILED, now=NOW, reason="x")
