"""Stage 2 (worker.py, stage2.py): an eligible call's analyse_stage2 queued on
its own queue once call.stage1 has gone, the job kept done with a stage2
field, and the task settling that field."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from arq import Retry

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.jobs import (
    JobStatus,
    JobStoreUnavailable,
    Stage2State,
    create_job,
    read_job,
    result_key,
)
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.units.call_intelligence import stage2, worker
from dodeal_ai.units.call_intelligence.admission import job_metadata
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    new_config_version,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.prompts import ESCALATIONS_TEMPLATE
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE, STAGE2_QUEUE
from dodeal_ai.units.call_intelligence.schemas import CallJobRequest
from dodeal_ai.units.call_intelligence.stage2 import RESULT_GONE, analyse_stage2
from dodeal_ai.units.call_intelligence.transcriber import Segment
from dodeal_ai.units.call_intelligence.worker import STAGE1, process_call
from dodeal_ai.workers import calls as calls_worker
from tests.conftest import RedisFakes
from tests.helpers.fake_llm import FakeLLM, json_response

HOST = "audio.tenant-a.example"
JOB = "job-1"
ON = {"calls_enabled": True, "audio_hosts": [HOST]}


def _say(start: float, speaker: str, text: str, confidence: float = 0.9) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=confidence,
    )


# An invented call.
SEGMENTS = (
    _say(0, "agent", "Good morning, this is the sales office about the villa."),
    _say(5, "lead", "I want a villa, my budget is 1,200,000 AED."),
)


class _Order:
    """Records each delivery and each stage-2 push, in the order they happen."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, job, event: str, config) -> bool:
        self.seen.append(event)
        return True


async def _resolve(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _audio(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=b"RIFF" + b"\x00" * 64, headers={"content-type": "audio/wav"}
    )


@pytest.fixture
async def ctx() -> AsyncIterator[dict[str, Any]]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_audio)) as http:
        yield {
            "http": http,
            "transcriber": FakeTranscriber(SEGMENTS),
            "resolve": _resolve,
            "deliver": _Order(),
            "llm": None,
        }


async def _push(duration: int = 150, **body: object) -> None:
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        ON,
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )
    request = CallJobRequest.model_validate(
        {
            "call_id": 7,
            "lead_id": 1656,
            "author_id": 27,
            "duration_seconds": duration,
            "recorded_at": "2026-09-23T08:00:00+00:00",
            "audio_url": f"https://{HOST}/calls/7.wav?sig=SIGNED",
            "audio_url_expires_at": (
                datetime.now(UTC) + timedelta(hours=2)
            ).isoformat(),
            **body,
        }
    )
    await create_job(
        "tenant-a",
        7,
        job_id=JOB,
        request_id="req-push",
        queue=NORMAL_QUEUE,
        metadata=job_metadata(request, parse_unit_b_section(ON)),
        now=datetime.now(UTC),
    )


async def _job():
    job = await read_job("tenant-a", JOB)
    assert job is not None
    return job


async def _stage2_jobs(redis_fakes: RedisFakes) -> list:
    return await redis_fakes.queue.queued_jobs(queue_name=STAGE2_QUEUE)


# --- the guard: stage 2 waits for stage 1, and only an eligible call gets it -------


async def test_an_eligible_call_queues_stage2_once_its_stage1_has_gone(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    real = worker.enqueue_stage2

    async def pushed(tenant: str, job_id: str) -> None:
        ctx["deliver"].seen.append("stage2_queued")
        await real(tenant, job_id)

    monkeypatch.setattr(worker, "enqueue_stage2", pushed)
    await _push()
    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.stage2) == (JobStatus.DONE, Stage2State.PENDING)
    assert ctx["deliver"].seen == [STAGE1, "stage2_queued"]
    (queued,) = await _stage2_jobs(redis_fakes)
    assert (queued.function, queued.args) == ("analyse_stage2", ("tenant-a", JOB))


@pytest.mark.parametrize(
    ("duration", "body", "confidence"),
    [
        (60, {}, 0.9),
        (150, {"call_outcome": "voicemail"}, 0.9),
        (150, {}, 0.3),
    ],
    ids=["short", "voicemail", "uncertain"],
)
async def test_an_ineligible_call_never_queues_stage2(
    ctx: dict,
    redis_fakes: RedisFakes,
    duration: int,
    body: dict,
    confidence: float,
) -> None:
    ctx["transcriber"] = FakeTranscriber(
        tuple(_say(s.start_s, s.speaker, s.text, confidence) for s in SEGMENTS)
    )
    await _push(duration, **body)
    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.stage2) == (JobStatus.DONE, Stage2State.NOT_ELIGIBLE)
    assert await _stage2_jobs(redis_fakes) == []
    assert ctx["deliver"].seen == [STAGE1]


async def test_a_queue_down_at_stage2_re_runs_and_never_holds_stage1(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    real = worker.enqueue_stage2
    outages = [QueueUnavailable()]

    async def flaky(tenant: str, job_id: str) -> None:
        if outages:
            raise outages.pop()
        await real(tenant, job_id)

    monkeypatch.setattr(worker, "enqueue_stage2", flaky)
    await _push()
    with pytest.raises(Retry):
        await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).stage2 is Stage2State.PENDING
    assert ctx["deliver"].seen == [STAGE1]

    await process_call(ctx, "tenant-a", JOB)
    assert len(await _stage2_jobs(redis_fakes)) == 1
    assert ctx["deliver"].seen == [STAGE1]


async def test_a_deadline_with_stage2_still_owed_re_runs_the_job(ctx: dict) -> None:
    await _push()
    job = await _job()
    await worker.transition(
        job,
        JobStatus.DONE,
        now=datetime.now(UTC),
        ttl_seconds=60,
        stage2=Stage2State.PENDING,
    )
    with pytest.raises(Retry):
        await worker._expired(ctx, "tenant-a", JOB, worker.CallRun())


# --- the task ------------------------------------------------------------------------

NONE_RAISED = {"objections": []}


def _stage2_ctx(*answers: object, job_try: int = 1) -> dict[str, Any]:
    llm = FakeLLM(*(json_response(a) for a in answers or (NONE_RAISED,)))
    llm.script_for(ESCALATIONS_TEMPLATE, json_response({"escalations": []}))
    return {"llm": llm, "job_try": job_try}


async def _done_and_pending(ctx: dict) -> None:
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).stage2 is Stage2State.PENDING


async def test_stage2_settles_done_on_the_stage1_transcript(
    ctx: dict, caplog: pytest.LogCaptureFixture
) -> None:
    await _done_and_pending(ctx)
    stage2_ctx = _stage2_ctx()
    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await analyse_stage2(stage2_ctx, "tenant-a", JOB)
    profiles = [c.profile for c in stage2_ctx["llm"].calls]
    assert profiles[:2] == ["unit_b.objections", "unit_b.escalations"]

    job = await _job()
    assert (job.status, job.stage2, job.stage2_reason) == (
        JobStatus.DONE,
        Stage2State.DONE,
        None,
    )
    (line,) = [r for r in caplog.records if r.getMessage() == "call_stage2_outcome"]
    assert (line.stage2, line.reason, line.levelno) == ("done", None, logging.INFO)


async def test_stage2_whose_stage1_result_expired_fails_with_its_reason(
    ctx: dict, redis_fakes: RedisFakes, caplog: pytest.LogCaptureFixture
) -> None:
    await _done_and_pending(ctx)
    await redis_fakes.jobs.delete(result_key("tenant-a", JOB))
    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await analyse_stage2(_stage2_ctx(), "tenant-a", JOB)

    job = await _job()
    assert (job.stage2, job.stage2_reason) == (Stage2State.FAILED, RESULT_GONE)
    (line,) = [r for r in caplog.records if r.getMessage() == "call_stage2_outcome"]
    assert line.levelno == logging.WARNING


async def test_stage2_not_pending_or_gone_is_left_alone(
    ctx: dict, caplog: pytest.LogCaptureFixture
) -> None:
    await _push(duration=60)
    await process_call(ctx, "tenant-a", JOB)
    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await analyse_stage2({}, "tenant-a", JOB)
        await analyse_stage2({}, "tenant-a", "job-never")
    assert (await _job()).stage2 is Stage2State.NOT_ELIGIBLE
    assert not [r for r in caplog.records if r.getMessage() == "call_stage2_outcome"]


async def test_a_worker_with_no_model_fails_stage2_and_pays_nothing(ctx: dict) -> None:
    await _done_and_pending(ctx)
    await analyse_stage2({"llm": None}, "tenant-a", JOB)
    job = await _job()
    assert (job.stage2, job.stage2_reason) == (Stage2State.FAILED, "llm_not_configured")


async def test_a_failed_pass_still_settles_stage2_done(
    ctx: dict, caplog: pytest.LogCaptureFixture
) -> None:
    await _done_and_pending(ctx)
    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await analyse_stage2(_stage2_ctx({}, {}), "tenant-a", JOB)
    assert (await _job()).stage2 is Stage2State.DONE
    (line,) = [r for r in caplog.records if r.getMessage() == "call_stage2_outcome"]
    assert line.part_reasons["objections"] == "objections_malformed_output"
    assert line.part_reasons["score"] == "scoring_off"


@pytest.mark.parametrize(
    ("stored", "fail", "reason", "delay"),
    [
        (
            {"tokens:calls:tenant:tenant-a": 20_000_000},
            False,
            "token_budget_exceeded",
            None,
        ),
        ({}, True, "cost_store_unavailable", 300),
    ],
    ids=["over-budget", "store-down"],
)
async def test_over_budget_or_store_down_waits_then_fails_on_the_last_run(
    ctx: dict,
    redis_fakes: RedisFakes,
    stored: dict,
    fail: bool,
    reason: str,
    delay: int | None,
) -> None:
    """Nothing is paid for; arq runs it again, and the last run gives up."""
    await _done_and_pending(ctx)
    redis_fakes.cost.store.update(stored)
    redis_fakes.cost.fail = fail
    first = _stage2_ctx()
    with pytest.raises(Retry) as again:
        await analyse_stage2(first, "tenant-a", JOB)
    expected = get_settings().cost_window_seconds if delay is None else delay
    assert again.value.defer_score == expected * 1000
    assert first["llm"].call_count == 0
    assert (await _job()).stage2 is Stage2State.PENDING

    last = _stage2_ctx(job_try=stage2.STAGE2_TRIES)
    await analyse_stage2(last, "tenant-a", JOB)
    job = await _job()
    assert (job.stage2, job.stage2_reason) == (Stage2State.FAILED, reason)
    assert last["llm"].call_count == 0


async def test_a_run_cut_off_by_its_deadline_runs_again_and_resumes(
    ctx: dict, monkeypatch
) -> None:
    await _done_and_pending(ctx)
    monkeypatch.setattr(stage2, "deadline_seconds", lambda: 0.2)
    held = {"llm": FakeLLM(hold_after=0), "job_try": 1}
    with pytest.raises(Retry):
        await analyse_stage2(held, "tenant-a", JOB)
    assert (await _job()).passes == {"objections": 1}

    monkeypatch.setattr(stage2, "deadline_seconds", lambda: 60)
    resumed = _stage2_ctx(job_try=2)
    await analyse_stage2(resumed, "tenant-a", JOB)
    job = await _job()
    assert (job.stage2, job.passes["objections"]) == (Stage2State.DONE, 2)


async def test_a_deadline_on_the_last_run_fails_stage2(ctx: dict, monkeypatch) -> None:
    await _done_and_pending(ctx)
    monkeypatch.setattr(stage2, "deadline_seconds", lambda: 0.2)
    held = {"llm": FakeLLM(hold_after=0), "job_try": stage2.STAGE2_TRIES}
    await analyse_stage2(held, "tenant-a", JOB)
    job = await _job()
    assert (job.stage2, job.stage2_reason) == (
        Stage2State.FAILED,
        "job_deadline_exceeded",
    )


async def test_a_timeout_that_is_not_the_deadline_is_an_error(
    ctx: dict, monkeypatch
) -> None:
    await _done_and_pending(ctx)

    async def slow(*args: object, **kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(stage2, "wave2", slow)
    with pytest.raises(TimeoutError):
        await analyse_stage2(_stage2_ctx(), "tenant-a", JOB)


async def test_stage2_that_stops_under_its_passes_settles_nothing(
    ctx: dict, monkeypatch
) -> None:
    from dodeal_ai.units.call_intelligence.paid import JobGone

    await _done_and_pending(ctx)

    async def gone(*args: object, **kwargs: object) -> None:
        raise JobGone()

    monkeypatch.setattr(stage2, "wave2", gone)
    await analyse_stage2(_stage2_ctx(), "tenant-a", JOB)
    assert (await _job()).stage2 is Stage2State.PENDING


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"signals": {"escalations": [{"type": "x"}, "junk"]}}, [{"type": "x"}]),
        ({"signals": None}, []),
        ({"signals": {"escalations": None}}, []),
        ({}, []),
    ],
)
def test_stage1_escalations_are_read_from_its_signals_block(
    result: dict, expected: list
) -> None:
    assert stage2._stage1_escalations(result) == expected


async def test_a_dead_job_store_is_retried_by_arq(monkeypatch) -> None:
    async def down(*args: object, **kwargs: object) -> None:
        raise JobStoreUnavailable()

    monkeypatch.setattr(stage2, "read_job", down)
    with pytest.raises(Retry):
        await analyse_stage2({}, "tenant-a", JOB)


# --- the worker ----------------------------------------------------------------------


def test_the_stage2_worker_runs_only_analyse_stage2_and_sweeps_nothing() -> None:
    built = calls_worker.worker_settings(STAGE2_QUEUE)
    assert [(f.name, f.max_tries) for f in built["functions"]] == [
        ("analyse_stage2", calls_worker.STAGE2_TRIES)
    ]
    assert (built["queue_name"], built["cron_jobs"]) == (STAGE2_QUEUE, [])
    assert calls_worker.QUEUES["stage2"] == STAGE2_QUEUE


async def test_the_stage2_worker_builds_a_model_client_and_no_transcriber(
    monkeypatch,
) -> None:
    for name, value in {
        "PROVIDER": "groq",
        "MODEL": "model-pinned-2026-01-01",
        "API_KEY": "test-only-key",
    }.items():
        monkeypatch.setenv(f"DODEAL_LLM_{name}", value)
    get_settings.cache_clear()
    built = calls_worker.worker_settings(STAGE2_QUEUE)
    ctx: dict[str, Any] = {}
    await built["on_startup"](ctx)
    try:
        assert "transcriber" not in ctx
        assert ctx["llm"] is not None
    finally:
        await built["on_shutdown"](ctx)
