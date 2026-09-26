"""Re-analysing a stored call (reanalysis.py): its transcript, never its audio,
one job per call, stages and versions, every event marked reanalysis."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.jobs import (
    JobStatus,
    Stage2State,
    clear_work,
    read_job,
    read_result,
    read_stage2_result,
)
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.main import app
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    new_config_version,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.delivery import event_body
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    EXTRAS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.queues import OVERNIGHT_QUEUE
from dodeal_ai.units.call_intelligence.reanalysis import admit_reanalysis
from dodeal_ai.units.call_intelligence.schemas import ReanalysisRequest
from dodeal_ai.units.call_intelligence.stage2 import analyse_stage2
from dodeal_ai.units.call_intelligence.transcriber import Transcript
from dodeal_ai.units.call_intelligence.worker import STAGE1, process_call
from tests.conftest import RedisFakes
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.wave2_answers import coaching_answer, extras_answer
from tests.unit.test_call_score import _checks
from tests.unit.test_call_stage1 import (
    EXTRACTION,
    ON,
    PROSE,
    SEGMENTS,
    _Deliveries,
    _resolve,
    _say,
)

URL = "/api/v1/calls/reanalysis"
CONFIG = parse_unit_b_section(ON)
STORED = Transcript.of(SEGMENTS, provider="gemini", model="gemini-3.5-transcribe")
SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-re"
).scope_for_author(27, budget="calls")


def _body(**changes: object) -> dict[str, Any]:
    body: dict[str, Any] = {
        "call_id": 7,
        "lead_id": 1656,
        "author_id": 27,
        "duration_seconds": 150,
        "recorded_at": "2026-09-23T08:00:00+04:00",
        "transcript": STORED.model_dump(mode="json"),
        "reason": "objection_list_changed",
        "stages": [1, 2],
    }
    return {**body, **changes}


async def _calls_on() -> None:
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        ON,
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )


@pytest.fixture
async def client(monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }


async def test_the_same_call_stages_and_versions_twice_are_one_job(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    """Idempotent per (tenant, call, stages, versions), on the overnight queue."""
    await _calls_on()
    first = await client.post(URL, json=_body(), headers=_headers())
    again = await client.post(URL, json=_body(), headers=_headers())
    other = await client.post(URL, json=_body(stages=[2]), headers=_headers())
    assert (first.status_code, again.status_code, other.status_code) == (202,) * 3
    assert (
        first.json()
        == again.json()
        == {"job_id": first.json()["job_id"], "status": "queued"}
    )
    assert other.json()["job_id"] != first.json()["job_id"]
    queued = await redis_fakes.queue.queued_jobs(queue_name=OVERNIGHT_QUEUE)
    assert len(queued) == 2
    for refused in (_body(stages=[1, 1]), _body(stages=[]), _body(audio_url="x")):
        response = await client.post(URL, json=refused, headers=_headers())
        assert response.status_code == 422


@pytest.fixture
async def ctx() -> AsyncIterator[dict[str, Any]]:
    async with httpx.AsyncClient() as http:
        yield {
            "http": http,
            "transcriber": FakeTranscriber(SEGMENTS),
            "resolve": _resolve,
            "deliver": _Deliveries(),
            "llm": FakeLLM(json_response(EXTRACTION), json_response(PROSE)),
        }


async def test_a_reanalysis_never_calls_the_transcriber(
    ctx: dict[str, Any], redis_fakes: RedisFakes
) -> None:
    """Stage 1 again from the stored transcript, delivered marked reanalysis."""
    await _calls_on()
    body = ReanalysisRequest.model_validate(_body(stages=[1]))
    accepted = await admit_reanalysis(SCOPE, body, CONFIG, now=datetime.now(UTC))
    await process_call(ctx, "tenant-a", accepted.job_id)

    assert ctx["transcriber"].calls == []
    job = await read_job("tenant-a", accepted.job_id)
    assert job is not None and job.status is JobStatus.DONE
    result = await read_result("tenant-a", accepted.job_id)
    assert result is not None and result["analysis"] is not None
    assert (
        result["transcript"]["segments"] == STORED.model_dump(mode="json")["segments"]
    )
    sent = json.loads(await event_body(job, STAGE1))
    assert sent["reanalysis"] is True and job.stage2 is Stage2State.NOT_ELIGIBLE


async def test_stage_2_alone_skips_stage_1s_passes_and_no_transcript_fails(
    ctx: dict[str, Any], redis_fakes: RedisFakes
) -> None:
    """Stage 1 keeps its signals only; a job without its transcript is failed."""
    await _calls_on()
    alone = ReanalysisRequest.model_validate(_body(stages=[2]))
    accepted = await admit_reanalysis(SCOPE, alone, CONFIG, now=datetime.now(UTC))
    await process_call(ctx, "tenant-a", accepted.job_id)
    result = await read_result("tenant-a", accepted.job_id)
    assert result is not None
    assert (result["analysis_reason"], ctx["llm"].call_count) == (
        "stage1_not_requested",
        0,
    )
    job = await read_job("tenant-a", accepted.job_id)
    assert job is not None and job.delivery is None
    assert job.stage2 is Stage2State.PENDING

    lost = await admit_reanalysis(
        SCOPE,
        ReanalysisRequest.model_validate(_body(call_id=8)),
        CONFIG,
        now=datetime.now(UTC),
    )
    await clear_work("tenant-a", lost.job_id)
    await process_call(ctx, "tenant-a", lost.job_id)
    failed = await read_job("tenant-a", lost.job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.reason == "reanalysis_transcript_missing"
    assert ctx["transcriber"].calls == []


async def test_a_reanalysis_marks_the_score_again_once_the_escalations_answer(
    ctx: dict[str, Any], redis_fakes: RedisFakes
) -> None:
    """D-75 on the same path: stage 2 of a re-analysis fails no_over_promise
    on the agent's verified promise, and stamps call_rubric_v2."""
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        {**ON, "scoring_enabled": True},
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )
    promise = "I guarantee this villa doubles in value within a year."
    said = (*SEGMENTS[:2], _say(10, "agent", promise), SEGMENTS[3])
    stored = Transcript.of(said, provider="gemini", model="gemini-3.5-transcribe")
    body = _body(stages=[2], transcript=stored.model_dump(mode="json"))
    accepted = await admit_reanalysis(
        SCOPE, ReanalysisRequest.model_validate(body), CONFIG, now=datetime.now(UTC)
    )
    await process_call(ctx, "tenant-a", accepted.job_id)
    flag = {
        "issue": "over_promise_or_guarantee",
        "quote": "I guarantee this villa doubles in value",
        "segment": "s3",
    }
    llm = FakeLLM(
        json_response({"objections": []}),
        json_response(_checks(courteous="Good morning")),
    )
    llm.script_for(ESCALATIONS_TEMPLATE, json_response({"escalations": [flag]}))
    llm.script_for(COACHING_TEMPLATE, json_response(coaching_answer(said[0].text)))
    llm.script_for(EXTRAS_TEMPLATE, json_response(extras_answer()))

    await analyse_stage2({"llm": llm}, "tenant-a", accepted.job_id)

    held = await read_stage2_result("tenant-a", accepted.job_id)
    assert held is not None and held["versions"]["rubric"] == "call_rubric_v2"
    professionalism = held["score"]["components"]["professionalism"]
    assert professionalism["checks"]["no_over_promise"] == {
        "answer": "no",
        "source": "code",
        **{name: flag[name] for name in ("quote", "segment")},
    }
    # Understanding 5 (listened_more alone), professionalism 11 of 15: 16 of 60.
    assert professionalism["mark"] == 11
    assert (held["score"]["raw"], held["score"]["total"]) == (16, 27)
    assert held["score"]["band"] == "coaching_required"
