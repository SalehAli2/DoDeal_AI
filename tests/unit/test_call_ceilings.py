"""Wave 2's output ceilings (register item 116): each pass has one for a
non-reasoning profile and one for a reasoning profile, the ceiling follows the
profile, and no reasoning text reaches a stage-2 part."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.jobs import JobStatus, Stage2State, create_job, read_job, transition
from dodeal_ai.core.llm import LLMResponse
from dodeal_ai.core.llm.profiles import task_ceiling
from dodeal_ai.units.call_intelligence import (
    coaching,
    escalations,
    extras,
    objections,
    score,
)
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    EXTRAS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
    SCORE_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import wave2
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.wave2_answers import coaching_answer, extras_answer

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)

# An invented call.
SEGMENTS = (
    Segment(
        start_s=0,
        end_s=5,
        speaker="agent",
        text="Good morning, calling about the villa.",
        language="en",
        confidence=0.9,
    ),
    Segment(
        start_s=5,
        end_s=10,
        speaker="lead",
        text="Yes, I am still looking for one.",
        language="en",
        confidence=0.9,
    ),
)
TRANSCRIPT = Transcript.of(SEGMENTS, provider="fake", model="fake")
CALL = CallText.of(TRANSCRIPT, country_code="971")

NO_CHECK = {"answer": "no", "quote": None, "segment": None}
Runner = Callable[..., Awaitable[tuple[BaseModel, LLMResponse]]]

# Each pass: its runner, its profile, an answer it accepts, its two ceilings.
PASSES: dict[str, tuple[Runner, str, dict[str, Any], int, int]] = {
    "objections": (
        objections.find_objections,
        "unit_b.objections",
        {"objections": []},
        2500,
        6000,
    ),
    "score": (
        score.ask_checks,
        "unit_b.score",
        {
            name: {**NO_CHECK, "answer": "yes"}
            if name in ("no_over_promise", "no_pressure")
            else NO_CHECK
            for name in score.CHECK_NAMES
        },
        2500,
        6000,
    ),
    "escalations": (
        escalations.find_flags,
        "unit_b.escalations",
        {"escalations": []},
        2500,
        6000,
    ),
    "coaching": (
        coaching.coach,
        "unit_b.coaching",
        coaching_answer(SEGMENTS[0].text),
        2000,
        4000,
    ),
    "extras": (extras.find_extras, "unit_b.extras", extras_answer(), 2000, 4000),
}

# The ceiling constants, as each pass module declares them beside itself.
CONSTANTS = {
    "objections": (
        objections.OBJECTIONS_MAX_OUTPUT_TOKENS,
        objections.OBJECTIONS_REASONING_MAX_OUTPUT_TOKENS,
    ),
    "score": (score.SCORE_MAX_OUTPUT_TOKENS, score.SCORE_REASONING_MAX_OUTPUT_TOKENS),
    "escalations": (
        escalations.ESCALATIONS_MAX_OUTPUT_TOKENS,
        escalations.ESCALATIONS_REASONING_MAX_OUTPUT_TOKENS,
    ),
    "coaching": (
        coaching.COACHING_MAX_OUTPUT_TOKENS,
        coaching.COACHING_REASONING_MAX_OUTPUT_TOKENS,
    ),
    "extras": (
        extras.EXTRAS_MAX_OUTPUT_TOKENS,
        extras.EXTRAS_REASONING_MAX_OUTPUT_TOKENS,
    ),
}


def _profiles(monkeypatch, **fields: object) -> Settings:
    """Every wave 2 profile set, each with `fields`."""
    table = {
        profile: {"provider": "openai", "model": "model-pinned", **fields}
        for _, profile, _, _, _ in PASSES.values()
    }
    monkeypatch.setenv("DODEAL_LLM_PROFILES", json.dumps(table))
    get_settings.cache_clear()
    return get_settings()


# --- the guard ------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(PASSES))
@pytest.mark.parametrize(
    ("fields", "reasons"),
    [({"reasoning_effort": "low"}, True), ({"temperature": 0}, False), (None, False)],
    ids=["reasoning", "non-reasoning", "fallback-pair"],
)
async def test_a_reasoning_profile_gets_the_larger_ceiling(
    monkeypatch, name: str, fields: dict | None, reasons: bool
) -> None:
    runner, _, answer, plain, reasoning = PASSES[name]
    settings = get_settings() if fields is None else _profiles(monkeypatch, **fields)
    llm = FakeLLM(json_response(answer))
    await runner(llm, CALL, scope=SCOPE, settings=settings)
    (sent,) = llm.calls
    assert sent.max_output_tokens == (reasoning if reasons else plain)


@pytest.mark.parametrize("name", list(PASSES))
def test_each_pass_declares_both_ceilings_beside_itself(name: str) -> None:
    _, _, _, plain, reasoning = PASSES[name]
    assert CONSTANTS[name] == (plain, reasoning)


def test_the_rule_reads_the_profile_and_never_raises() -> None:
    settings = get_settings()
    assert settings.llm_provider is None
    assert task_ceiling(settings, "unit_b.coaching", plain=1, reasoning=2) == 1


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
    return await read_job("tenant-a", "job-1")


async def test_no_reasoning_reaches_a_stage2_part(monkeypatch) -> None:
    """Reasoning profiles, answers that spent reasoning tokens: the parts hold
    the answers and nothing else, and a `reasoning` field is malformed."""
    settings = _profiles(monkeypatch, reasoning_effort="high")
    llm = FakeLLM()
    for template, answer in (
        (OBJECTIONS_TEMPLATE, {"objections": []}),
        (SCORE_TEMPLATE, PASSES["score"][2]),
        (ESCALATIONS_TEMPLATE, {"escalations": []}),
        (COACHING_TEMPLATE, PASSES["coaching"][2]),
        (EXTRAS_TEMPLATE, extras_answer()),
    ):
        llm.script_for(template, json_response(answer, reasoning_tokens=900))
    wave = await wave2(
        llm,
        await _done_job(),
        CallsConfig(),
        TRANSCRIPT,
        work={},
        scope=SCOPE,
        settings=settings,
        usage=PassUsage(),
        eligible=True,
        stage1_escalations=[],
    )
    assert "reasoning" not in json.dumps(wave.parts)
    assert [call.max_output_tokens for call in llm.calls] == [6000, 6000, 4000, 4000]


@pytest.mark.parametrize(
    "schema",
    [
        objections.Objections,
        score.ScoreChecks,
        escalations.Flags,
        coaching.Coaching,
        extras.Extras,
    ],
)
def test_an_answer_carrying_its_reasoning_is_refused(schema: type[BaseModel]) -> None:
    name = next(k for k, v in PASSES.items() if v[0].__module__ == schema.__module__)
    with pytest.raises(ValidationError):
        schema.model_validate({**PASSES[name][2], "reasoning": "hidden thoughts"})
