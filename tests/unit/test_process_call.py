"""process_call (register items 35, 36): free outcomes first, budgets before
spend, at most two transcriptions, CALL_MAX_TRIES then dead_letter, and a crash
after the transcript is stored retries delivery only."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from arq import Retry

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.core.jobs import (
    JobStatus,
    create_job,
    job_key,
    read_job,
    read_result,
)
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.units.call_intelligence import worker
from dodeal_ai.units.call_intelligence.admission import job_metadata
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    new_config_version,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE
from dodeal_ai.units.call_intelligence.schemas import CallJobRequest
from dodeal_ai.units.call_intelligence.transcriber import (
    Segment,
    TranscriptionError,
)
from dodeal_ai.units.call_intelligence.worker import (
    FAILED,
    STAGE1,
    TOO_SHORT,
    process_call,
)
from dodeal_ai.workers import calls as calls_worker
from tests.conftest import RedisFakes

HOST = "audio.tenant-a.example"
PUBLIC = "93.184.216.34"
AUDIO = b"RIFF" + b"\x00" * 256
JOB = "job-1"
ON = {"calls_enabled": True, "audio_hosts": [HOST]}
REPO_ROOT = Path(__file__).resolve().parents[2]


class _Source:
    """The recording's host: answers every GET with `answer`, and counts them."""

    def __init__(self, answer: Callable[[], httpx.Response]) -> None:
        self.answer = answer
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return self.answer()


def _audio() -> httpx.Response:
    return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/wav"})


class _Deliveries:
    """Records every event; raises `crash` on the calls it is told to."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.crash_on: set[int] = set()

    async def __call__(self, job, event: str, config) -> bool:
        self.events.append(event)
        if len(self.events) in self.crash_on:
            raise RuntimeError("the worker died mid-delivery")
        return True


async def _resolve(host: str, port: int) -> list[str]:
    assert host == HOST
    return [PUBLIC]


@pytest.fixture
def source() -> _Source:
    return _Source(_audio)


@pytest.fixture
async def ctx(source: _Source) -> AsyncIterator[dict[str, Any]]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(source)) as http:
        yield {
            "http": http,
            "transcriber": FakeTranscriber(),
            "resolve": _resolve,
            "deliver": _Deliveries(),
        }


async def _calls_on(**changes: object) -> None:
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        {**ON, **changes},
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )


async def _push(**changes: object) -> None:
    body = CallJobRequest.model_validate(
        {
            "call_id": 7,
            "lead_id": 1656,
            "author_id": 27,
            "duration_seconds": 150,
            "recorded_at": "2026-09-23T08:00:00+00:00",
            "audio_url": f"https://{HOST}/calls/7.wav?sig=SIGNED",
            "audio_url_expires_at": (
                datetime.now(UTC) + timedelta(hours=2)
            ).isoformat(),
            "language_hint": "mixed",
            **changes,
        }
    )
    await create_job(
        "tenant-a",
        7,
        job_id=JOB,
        request_id="req-push",
        queue=NORMAL_QUEUE,
        metadata=job_metadata(body, parse_unit_b_section(ON)),
        now=datetime.now(UTC),
    )


async def _job():
    job = await read_job("tenant-a", JOB)
    assert job is not None
    return job


# --- the paid path --------------------------------------------------------------


async def test_a_call_is_downloaded_charged_transcribed_stored_and_done(
    ctx: dict, source: _Source, redis_fakes: RedisFakes
) -> None:
    await _calls_on()
    await _push()

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.attempts, job.reason) == (JobStatus.DONE, 1, None)
    result = await read_result("tenant-a", JOB)
    assert result is not None
    assert result["eligible_for_full_analysis"] is True
    assert result["outcome_label"] is None
    assert result["transcript"]["language_profile"] == "mixed"
    assert ctx["deliver"].events == [STAGE1]
    assert [c.language_hint for c in ctx["transcriber"].calls] == ["mixed"]
    assert source.requests == 1
    assert redis_fakes.cost.store["audio_seconds:calls:tenant:tenant-a"] == 150


@pytest.mark.parametrize(
    ("changes", "segments"),
    [
        ({"duration_seconds": 90}, None),
        (
            {},
            (
                Segment(
                    start_s=0,
                    end_s=60,
                    speaker="agent",
                    text="invented",
                    language="en",
                    confidence=0.2,
                ),
            ),
        ),
    ],
    ids=["under-scoring-min", "uncertain"],
)
async def test_short_or_uncertain_calls_are_not_eligible_for_full_analysis(
    ctx: dict, changes: dict, segments: tuple | None
) -> None:
    await _calls_on()
    await _push(**changes)
    if segments is not None:
        ctx["transcriber"] = FakeTranscriber(segments)

    await process_call(ctx, "tenant-a", JOB)

    result = await read_result("tenant-a", JOB)
    assert result is not None and result["eligible_for_full_analysis"] is False
    assert (await _job()).status is JobStatus.DONE


# --- the free outcomes (item 36) -------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"duration_seconds": 29}, TOO_SHORT),
        ({"call_outcome": "voicemail"}, "voicemail"),
        ({"call_outcome": "no_answer"}, "no_answer"),
    ],
)
async def test_a_short_or_unanswered_call_is_done_with_its_label_unpaid(
    ctx: dict, source: _Source, redis_fakes: RedisFakes, changes: dict, label: str
) -> None:
    await _calls_on()
    await _push(**changes)

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason) == (JobStatus.DONE, label)
    result = await read_result("tenant-a", JOB)
    assert result is not None
    assert (result["outcome_label"], result["transcript"]) == (label, None)
    assert source.requests == 0 and ctx["transcriber"].calls == []
    assert redis_fakes.cost.mgets == [] and redis_fakes.cost.store == {}
    assert ctx["deliver"].events == [STAGE1]


# --- the guard: a stored transcript is never re-paid ----------------------------


async def test_a_crash_after_transcription_retries_delivery_only(
    ctx: dict, source: _Source
) -> None:
    await _calls_on()
    await _push()
    ctx["deliver"].crash_on = {1}

    with pytest.raises(RuntimeError):
        await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).status is JobStatus.DELIVERING

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.attempts) == (JobStatus.DONE, 2)
    assert len(ctx["transcriber"].calls) == 1 and source.requests == 1
    assert ctx["deliver"].events == [STAGE1, STAGE1]


class _DyingTranscriber(FakeTranscriber):
    """Counts the call, then dies the way a killed worker does mid-request."""

    async def transcribe(self, audio_path: Path, *, language_hint: str | None):
        await super().transcribe(audio_path, language_hint=language_hint)
        raise RuntimeError("the worker died mid-transcription")


async def test_an_interrupted_job_is_transcribed_again_once_then_dead_letters(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    """Two paid transcriptions, each charged, then transcription_interrupted."""
    await _calls_on()
    await _push()
    ctx["transcriber"] = _DyingTranscriber()

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await process_call(ctx, "tenant-a", JOB)
        assert (await _job()).status is JobStatus.TRANSCRIBING
    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason, job.attempts, job.transcriptions) == (
        JobStatus.DEAD_LETTER,
        "transcription_interrupted",
        2,
        2,
    )
    assert len(ctx["transcriber"].calls) == 2
    assert redis_fakes.cost.store["audio_seconds:calls:tenant:tenant-a"] == 300
    assert ctx["deliver"].events == [FAILED]


async def test_an_interrupted_job_retried_once_can_still_succeed(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    """The retry that lands is done, with both attempts and both starts counted."""
    await _calls_on()
    await _push()
    await redis_fakes.jobs.hset(
        job_key("tenant-a", JOB),
        mapping={"status": "transcribing", "attempts": "1", "transcriptions": "1"},
    )

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.attempts, job.transcriptions) == (JobStatus.DONE, 2, 2)
    assert len(ctx["transcriber"].calls) == 1


# --- tries, failures and dead letters (item 35) ---------------------------------


async def test_a_flaky_source_is_retried_then_dead_lettered_at_the_cap(
    ctx: dict, source: _Source
) -> None:
    await _calls_on()
    await _push()
    source.answer = lambda: httpx.Response(503)

    with pytest.raises(Retry) as retried:
        await process_call(ctx, "tenant-a", JOB)
    job = await _job()
    assert (job.status, job.reason, job.attempts) == (
        JobStatus.QUEUED,
        "audio_source_unavailable",
        1,
    )
    assert retried.value.defer_score == worker.RETRY_DELAY_SECONDS * 1000

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason, job.attempts) == (
        JobStatus.DEAD_LETTER,
        "audio_source_unavailable",
        2,
    )
    assert ctx["transcriber"].calls == []
    assert ctx["deliver"].events == [FAILED]


async def test_an_expired_link_fails_and_is_never_retried(
    ctx: dict, source: _Source
) -> None:
    await _calls_on()
    await _push(audio_url_expires_at="2026-01-01T00:00:00+00:00")

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason) == (JobStatus.FAILED, "audio_link_expired")
    assert source.requests == 0
    assert ctx["deliver"].events == [FAILED]


async def test_a_failed_transcription_is_retried_once_then_dead_lettered(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    """One retry as an attempt, its seconds charged again, then call.failed."""
    await _calls_on()
    await _push()
    ctx["transcriber"] = FakeTranscriber(
        fail=TranscriptionError("stt_timeout", retryable=True)
    )

    with pytest.raises(Retry) as retried:
        await process_call(ctx, "tenant-a", JOB)
    job = await _job()
    assert (job.status, job.reason, job.attempts) == (
        JobStatus.QUEUED,
        "stt_timeout",
        1,
    )
    assert retried.value.defer_score == worker.RETRY_DELAY_SECONDS * 1000

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason, job.attempts, job.transcriptions) == (
        JobStatus.DEAD_LETTER,
        "stt_timeout",
        2,
        2,
    )
    assert len(ctx["transcriber"].calls) == 2
    assert redis_fakes.cost.store["audio_seconds:calls:tenant:tenant-a"] == 300
    assert ctx["deliver"].events == [FAILED]


async def test_a_failure_the_same_audio_cannot_pass_is_never_retried(
    ctx: dict,
) -> None:
    """A non-retryable failure fails at once: a retry would pay for nothing."""
    await _calls_on()
    await _push()
    ctx["transcriber"] = FakeTranscriber(
        fail=TranscriptionError("audio_unsupported", retryable=False)
    )

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason) == (JobStatus.FAILED, "audio_unsupported")
    assert len(ctx["transcriber"].calls) == 1


async def test_a_failed_transcription_on_the_last_attempt_dead_letters(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    """A download retry spent the spare attempt, so there is no second call."""
    await _calls_on()
    await _push()
    await redis_fakes.jobs.hset(job_key("tenant-a", JOB), "attempts", "1")
    ctx["transcriber"] = FakeTranscriber(
        fail=TranscriptionError("stt_unavailable", retryable=True)
    )

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason) == (JobStatus.DEAD_LETTER, "stt_unavailable")
    assert len(ctx["transcriber"].calls) == 1


async def test_a_job_gone_terminal_before_transcription_is_not_paid_for(
    ctx: dict, monkeypatch
) -> None:
    """No count, no transcription, and nothing sent for a job that is gone."""
    await _calls_on()
    await _push()

    async def gone(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker, "start_transcription", gone)
    await process_call(ctx, "tenant-a", JOB)
    assert ctx["transcriber"].calls == []
    assert ctx["deliver"].events == []


async def test_a_job_at_its_tries_on_entry_is_dead_lettered(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    await _calls_on()
    await _push()
    await redis_fakes.jobs.hset(job_key("tenant-a", JOB), "attempts", "2")

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason) == (JobStatus.DEAD_LETTER, "max_tries_exceeded")


async def test_a_claim_lost_at_the_cap_dead_letters(ctx: dict, monkeypatch) -> None:
    await _calls_on()
    await _push()

    async def at_cap(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(worker, "claim_attempt", at_cap)
    await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).status is JobStatus.DEAD_LETTER


async def test_a_claim_on_a_job_gone_terminal_does_nothing(
    ctx: dict, monkeypatch
) -> None:
    await _calls_on()
    await _push()

    async def gone(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker, "claim_attempt", gone)
    await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).status is JobStatus.QUEUED
    assert ctx["deliver"].events == []


async def test_calls_switched_off_after_the_push_fail_the_job(ctx: dict) -> None:
    await _calls_on()
    await _push()
    await _calls_on(calls_enabled=False)

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason) == (JobStatus.FAILED, "calls_not_enabled")
    assert ctx["transcriber"].calls == []


async def test_a_terminal_or_missing_job_is_left_alone(ctx: dict) -> None:
    await _calls_on()
    await process_call(ctx, "tenant-a", "no-such-job")
    await _push(call_outcome="voicemail")
    await process_call(ctx, "tenant-a", JOB)
    await process_call(ctx, "tenant-a", JOB)
    assert ctx["deliver"].events == [STAGE1]


async def test_a_second_failure_on_a_terminal_job_sends_nothing(ctx: dict) -> None:
    await _calls_on()
    await _push()
    job = await _job()
    await worker._fail(ctx, job, parse_unit_b_section(ON), "first", dead=False)
    await worker._fail(ctx, job, parse_unit_b_section(ON), "second", dead=True)
    assert ((await _job()).reason, ctx["deliver"].events) == ("first", [FAILED])


async def test_a_dead_job_store_is_retried_by_arq(ctx: dict, monkeypatch) -> None:
    from dodeal_ai.core.jobs import JobStoreUnavailable

    async def down(*args: object, **kwargs: object) -> None:
        raise JobStoreUnavailable()

    monkeypatch.setattr(worker, "read_job", down)
    with pytest.raises(Retry):
        await process_call(ctx, "tenant-a", JOB)


# --- budgets (core(105)) ----------------------------------------------------------


def _resume_delays(queued: list) -> list[float]:
    """Seconds each queued resume was deferred by, in queue order."""
    return [(j.score - j.enqueue_time.timestamp() * 1000) / 1000 for j in queued]


@pytest.mark.parametrize(
    ("stored", "fail", "reason", "delay"),
    [
        (
            {"tokens:calls:tenant:tenant-a": 20_000_000},
            False,
            "token_budget_exceeded",
            None,
        ),
        (
            {"audio_seconds:calls:tenant:tenant-a": 360_000},
            False,
            "audio_budget_exceeded",
            None,
        ),
        ({}, True, "cost_store_unavailable", worker.OUTAGE_DELAY_SECONDS),
    ],
)
async def test_over_budget_or_store_down_pauses_before_any_spend(
    ctx: dict,
    source: _Source,
    redis_fakes: RedisFakes,
    stored: dict,
    fail: bool,
    reason: str,
    delay: int | None,
) -> None:
    """Over budget waits for the window; a store outage waits 300 s."""
    await _calls_on()
    await _push()
    redis_fakes.cost.store.update(stored)
    redis_fakes.cost.fail = fail

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.reason, job.pauses, job.attempts) == (
        JobStatus.PAUSED_BUDGET,
        reason,
        1,
        0,
    )
    assert source.requests == 0 and ctx["transcriber"].calls == []
    (resumed,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)
    assert resumed.args == ("tenant-a", JOB)
    expected = get_settings().cost_window_seconds if delay is None else delay
    (waited,) = _resume_delays([resumed])
    assert expected - 5 <= waited <= expected + 5


async def test_two_outage_pauses_then_success_cost_no_attempt(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    """Pauses at the charge give their attempt back and back off 300 s, 600 s."""
    await _calls_on()
    await _push()
    from dodeal_ai.core.cost.limiter import CallsBudgetPaused

    real_charge = worker.charge_audio_seconds
    outages = [CallsBudgetPaused("cost_store_unavailable")] * 2

    async def flaky(*args: Any, **kwargs: Any) -> int:
        if outages:
            raise outages.pop()
        return await real_charge(*args, **kwargs)

    monkeypatch.setattr(worker, "charge_audio_seconds", flaky)
    for pauses in (1, 2):
        await process_call(ctx, "tenant-a", JOB)
        job = await _job()
        assert (job.status, job.attempts, job.pauses, job.outages) == (
            JobStatus.PAUSED_BUDGET,
            0,
            pauses,
            pauses,
        )
    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.attempts) == (JobStatus.DONE, 1)
    queued = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)
    delays = sorted(_resume_delays(queued))
    assert [round(d, -1) for d in delays] == [300, 600]
    assert len(ctx["transcriber"].calls) == 1


def test_an_outage_backs_off_doubling_to_the_cap_and_over_budget_waits_the_window():
    """300, 600, 1200, 2400, then 3600 for good; over budget is the window."""
    delays = [
        worker.pause_delay("cost_store_unavailable", n, window_seconds=86_400)
        for n in (1, 2, 3, 4, 5, 6, 1_000)
    ]
    assert delays == [300, 600, 1200, 2400, 3600, 3600, 3600]
    assert (
        worker.pause_delay("audio_budget_exceeded", 3, window_seconds=86_400) == 86_400
    )


async def test_a_store_down_at_the_audio_charge_pauses_before_transcribing(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    await _calls_on()
    await _push()
    from dodeal_ai.core.cost.limiter import CallsBudgetPaused

    async def refused(*args: object, **kwargs: object) -> int:
        raise CallsBudgetPaused("cost_store_unavailable")

    monkeypatch.setattr(worker, "charge_audio_seconds", refused)
    await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).status is JobStatus.PAUSED_BUDGET
    assert ctx["transcriber"].calls == []


async def test_a_pause_with_the_queue_down_is_retried(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    from dodeal_ai.core.errors import QueueUnavailable

    await _calls_on()
    await _push()
    redis_fakes.cost.fail = True

    async def no_queue(*args: object, **kwargs: object) -> None:
        raise QueueUnavailable()

    monkeypatch.setattr(worker, "enqueue_call", no_queue)
    with pytest.raises(Retry):
        await process_call(ctx, "tenant-a", JOB)


async def test_a_pause_of_a_job_gone_terminal_queues_nothing(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    await _calls_on()
    await _push()
    job = await _job()
    await redis_fakes.jobs.hset(job_key("tenant-a", JOB), "status", "done")
    await worker._pause(job, "token_budget_exceeded")
    assert await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE) == []


# --- the worker process -----------------------------------------------------------


async def test_the_worker_settings_carry_one_more_arq_try_than_the_job() -> None:
    fake = FakeTranscriber()
    built = calls_worker.worker_settings(NORMAL_QUEUE, transcriber=fake)
    assert [(f.name, f.max_tries) for f in built["functions"]] == [
        ("process_call", 3),
        ("deliver_callback", 2),
    ]
    assert built["queue_name"] == NORMAL_QUEUE

    ctx: dict[str, Any] = {}
    await built["on_startup"](ctx)
    assert ctx["transcriber"] is fake
    assert callable(ctx["deliver"])
    assert isinstance(ctx["http"], httpx.AsyncClient)
    assert ctx["http"].follow_redirects is False
    await built["on_shutdown"](ctx)
    assert ctx["http"].is_closed
    await built["on_shutdown"]({})


async def test_a_worker_with_no_transcriber_refuses_to_start() -> None:
    built = calls_worker.worker_settings(NORMAL_QUEUE)
    with pytest.raises(ConfigError, match="^stt_not_configured$"):
        await built["on_startup"]({})


def test_the_command_line_names_one_of_three_queues(monkeypatch) -> None:
    ran: list[dict] = []
    monkeypatch.setattr(calls_worker, "run_worker", ran.append)
    assert calls_worker.main([]) == 2
    assert calls_worker.main(["urgent"]) == 2
    assert calls_worker.main(["overnight"]) == 0
    assert ran[0]["queue_name"] == "arq:calls:overnight"


def test_importing_the_call_workers_reads_no_settings() -> None:
    """The wheel check and pytest collection import it with no environment."""
    probe = (
        "import dodeal_ai.workers.calls\n"
        "from dodeal_ai.core.config import get_settings\n"
        "assert get_settings.cache_info().currsize == 0\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("DODEAL_")}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_with_no_deliverer_a_job_is_done_with_nothing_to_send(ctx: dict) -> None:
    await _calls_on()
    await _push(call_outcome="voicemail")
    del ctx["deliver"]
    await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).status is JobStatus.DONE
