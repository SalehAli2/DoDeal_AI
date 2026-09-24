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

from dodeal_ai.core.callbacks import CALL_FAILED, CALL_STAGE2
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.jobs import (
    STAGE2_PENDING_KEY,
    DeliveryState,
    JobStatus,
    JobStoreUnavailable,
    Stage2State,
    create_job,
    job_key,
    read_job,
    read_stage2_result,
    result_key,
    settle_stage2,
    stage2_result_key,
)
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.units.call_intelligence import queues, stage2, sweep, worker
from dodeal_ai.units.call_intelligence.admission import job_metadata
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    new_config_version,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    EXTRAS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE, STAGE2_QUEUE
from dodeal_ai.units.call_intelligence.schemas import CallJobRequest
from dodeal_ai.units.call_intelligence.stage2 import RESULT_GONE, analyse_stage2
from dodeal_ai.units.call_intelligence.transcriber import Segment
from dodeal_ai.units.call_intelligence.worker import STAGE1, process_call
from dodeal_ai.workers import calls as calls_worker
from tests.conftest import RedisFakes
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.wave2_answers import coaching_answer, extras_answer

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
    llm.script_for(COACHING_TEMPLATE, json_response(coaching_answer(SEGMENTS[0].text)))
    llm.script_for(EXTRAS_TEMPLATE, json_response(extras_answer()))
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
    assert stage2_ctx["llm"].profiles == [
        "unit_b.objections",
        "unit_b.escalations",
        "unit_b.coaching",
        "unit_b.extras",
    ]

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
    assert line.part_reasons == {
        "objections": "objections_malformed_output",
        "score": "scoring_off",
    }


def _delivering(*answers: object) -> dict[str, Any]:
    return {**_stage2_ctx(*answers), "deliver": _Order()}


async def test_stage2_is_held_and_delivered_as_call_stage2(
    ctx: dict, redis_fakes: RedisFakes, caplog: pytest.LogCaptureFixture
) -> None:
    await _done_and_pending(ctx)
    stage2_ctx = _delivering()
    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await analyse_stage2(stage2_ctx, "tenant-a", JOB)

    assert stage2_ctx["deliver"].seen == [CALL_STAGE2]
    held = await read_stage2_result("tenant-a", JOB)
    assert held is not None
    parts = ("objections", "score", "escalations", "coaching", "extras")
    assert set(held) == {"stage", "call_id", "reasons", "versions", *parts}
    assert (held["stage"], held["call_id"], held["score"]) == (2, 7, None)
    assert all(held[name] is not None for name in parts if name != "score")
    assert held["reasons"] == {"score": "scoring_off"}
    assert held["versions"] == {
        "prompt": "unit_b_prompts_v2",
        "objection_list": "objection_list_v1",
        "rubric": "call_rubric_v1",
        "tone_list": "tone_list_v1",
        "model": dict.fromkeys(
            ("objections", "escalations", "coaching", "extras"), "fake-model-pinned"
        ),
    }
    ttl = await redis_fakes.jobs.ttl(stage2_result_key("tenant-a", JOB))
    assert 0 < ttl <= 259_200
    (line,) = [r for r in caplog.records if r.getMessage() == "call_stage2_outcome"]
    spent = {"input": 100, "output": 20, "calls": 1}
    passed = ("objections", "escalations", "coaching", "extras")
    assert line.pass_tokens == dict.fromkeys(passed, spent)
    assert line.pass_reasoning_tokens == dict.fromkeys(passed, 0)


async def test_one_failed_pass_still_delivers_the_others(ctx: dict) -> None:
    """The guard: the failed part is null with its reason, the rest goes."""
    await _done_and_pending(ctx)
    stage2_ctx = _delivering({}, {})
    await analyse_stage2(stage2_ctx, "tenant-a", JOB)

    assert stage2_ctx["deliver"].seen == [CALL_STAGE2]
    held = await read_stage2_result("tenant-a", JOB)
    assert held is not None
    assert (held["objections"], held["reasons"]["objections"]) == (
        None,
        "objections_malformed_output",
    )
    assert all(held[name] is not None for name in ("escalations", "coaching", "extras"))


async def test_a_failed_stage2_sends_call_failed_and_holds_no_result(ctx: dict) -> None:
    await _done_and_pending(ctx)
    stage2_ctx = {"llm": None, "deliver": _Order()}
    await analyse_stage2(stage2_ctx, "tenant-a", JOB)
    assert stage2_ctx["deliver"].seen == [CALL_FAILED]
    assert await read_stage2_result("tenant-a", JOB) is None


@pytest.mark.parametrize(
    ("state", "event"),
    [(Stage2State.DONE, CALL_STAGE2), (Stage2State.FAILED, CALL_FAILED)],
)
async def test_a_rerun_sends_a_stage2_event_left_pending(
    ctx: dict, state: Stage2State, event: str
) -> None:
    """The last run died between the settle and the send."""
    await _done_and_pending(ctx)
    await settle_stage2(
        await _job(), state, now=datetime.now(UTC), delivery=DeliveryState.PENDING
    )
    stage2_ctx = {"deliver": _Order()}
    await analyse_stage2(stage2_ctx, "tenant-a", JOB)
    assert stage2_ctx["deliver"].seen == [event]


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
    assert (job.stage2, job.passes) == (
        Stage2State.DONE,
        {"objections": 2, "escalations": 1, "coaching": 1, "extras": 1},
    )


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


# --- a lost stage 2: the sweep ---------------------------------------------------------


def _hours_on(hours: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


async def _drop(redis_fakes: RedisFakes, suffix: str) -> None:
    """arq loses the stage-2 entry under `suffix`."""
    assert await redis_fakes.queue.delete(f"arq:job:tenant-a:{JOB}:{suffix}") == 1
    await redis_fakes.queue.zrem(STAGE2_QUEUE, f"tenant-a:{JOB}:{suffix}")


async def _pending_members(redis_fakes: RedisFakes) -> list[bytes]:
    return await redis_fakes.jobs.zrange(STAGE2_PENDING_KEY, 0, -1)


async def test_a_lost_stage2_is_requeued_once_then_failed_never_pending_forever(
    ctx: dict, redis_fakes: RedisFakes, caplog: pytest.LogCaptureFixture
) -> None:
    await _done_and_pending(ctx)
    assert await sweep.sweep(now=_hours_on(1.9), deliver=ctx["deliver"]) == 0
    assert await sweep.sweep(now=_hours_on(2.1), deliver=ctx["deliver"]) == 0

    await _drop(redis_fakes, "stage2")
    assert await sweep.sweep(now=_hours_on(2.1), deliver=ctx["deliver"]) == 1
    (requeued,) = await _stage2_jobs(redis_fakes)
    assert (requeued.function, requeued.args) == ("analyse_stage2", ("tenant-a", JOB))
    assert (await _job()).stage2_sweeps == 1
    assert await sweep.sweep(now=_hours_on(2.1), deliver=ctx["deliver"]) == 0
    assert await sweep.sweep(now=_hours_on(4.2), deliver=ctx["deliver"]) == 0

    await _drop(redis_fakes, "stage2:sweep")
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.unit_b"):
        assert await sweep.sweep(now=_hours_on(4.2), deliver=ctx["deliver"]) == 1
    job = await _job()
    assert (job.status, job.stage2, job.stage2_reason) == (
        JobStatus.DONE,
        Stage2State.FAILED,
        "stage2_lost",
    )
    assert ctx["deliver"].seen == [STAGE1, CALL_FAILED]
    assert await _pending_members(redis_fakes) == []
    assert await sweep.sweep(now=_hours_on(9), deliver=ctx["deliver"]) == 0
    (line,) = [r for r in caplog.records if r.getMessage() == "call_stage2_lost"]
    assert (line.job_id, line.reason_code) == (JOB, "stage2_lost")


async def test_a_requeued_stage2_that_runs_settles_and_leaves_the_sweep(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    await _done_and_pending(ctx)
    await _drop(redis_fakes, "stage2")
    assert await sweep.sweep(now=_hours_on(2.1)) == 1
    await analyse_stage2(_stage2_ctx(), "tenant-a", JOB)
    assert (await _job()).stage2 is Stage2State.DONE
    assert await _pending_members(redis_fakes) == []
    await _drop(redis_fakes, "stage2:sweep")
    assert await sweep.sweep(now=_hours_on(9)) == 0


async def test_a_stage2_whose_job_expired_leaves_the_pending_set(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    await _done_and_pending(ctx)
    await redis_fakes.jobs.delete(job_key("tenant-a", JOB))
    assert await sweep.sweep(now=_hours_on(9)) == 0
    assert await _pending_members(redis_fakes) == []


async def test_a_stage2_settled_under_the_sweep_is_not_failed_again(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    """Settled between the sweep's look and its failing it: nothing is sent."""
    await _done_and_pending(ctx)
    await _drop(redis_fakes, "stage2")
    assert await sweep.sweep(now=_hours_on(2.1)) == 1
    await _drop(redis_fakes, "stage2:sweep")
    real = sweep.read_job

    async def settled_meanwhile(tenant: str, job_id: str):
        job = await real(tenant, job_id)
        await settle_stage2(job, Stage2State.DONE, now=datetime.now(UTC))
        return job

    monkeypatch.setattr(sweep, "read_job", settled_meanwhile)
    assert await sweep.sweep(now=_hours_on(4.2), deliver=ctx["deliver"]) == 0
    assert (await _job()).stage2 is Stage2State.DONE
    assert ctx["deliver"].seen == [STAGE1]

    async def gone(tenant: str, job_id: str) -> None:
        return None

    await redis_fakes.jobs.zadd(STAGE2_PENDING_KEY, {f"tenant-a:{JOB}": 0})
    await redis_fakes.jobs.hset(job_key("tenant-a", JOB), "stage2", "pending")
    monkeypatch.setattr(sweep, "read_job", gone)
    assert await sweep.sweep(now=_hours_on(4.2), deliver=ctx["deliver"]) == 0


async def test_a_sweep_that_cannot_ask_arq_about_stage2_stops(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    await _done_and_pending(ctx)

    async def no_queue(*args: object, **kwargs: object) -> bool:
        raise QueueUnavailable()

    monkeypatch.setattr(sweep, "stage2_on_the_queue", no_queue)
    assert await sweep.sweep(now=_hours_on(2.1)) == 0
    assert (await _job()).stage2_sweeps == 0


async def test_arq_is_asked_under_both_stage2_ids(redis_fakes: RedisFakes) -> None:
    assert not await queues.stage2_on_the_queue("tenant-a", JOB)
    await queues.enqueue_stage2("tenant-a", JOB, sweep=True)
    assert await queues.stage2_on_the_queue("tenant-a", JOB)
    await _drop(redis_fakes, "stage2:sweep")
    await redis_fakes.queue.set(f"arq:in-progress:tenant-a:{JOB}:stage2", b"1")
    assert await queues.stage2_on_the_queue("tenant-a", JOB)


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
