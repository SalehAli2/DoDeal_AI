"""process_call (register items 35, 36): free outcomes first, budgets before
spend, at most two transcriptions, CALL_MAX_TRIES then dead_letter, and a crash
after the transcript is stored retries delivery only."""

from __future__ import annotations

import asyncio
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
    DeliveryState,
    JobStatus,
    create_job,
    job_key,
    read_job,
    read_result,
)
from dodeal_ai.core.llm import OpenAICompatibleClient
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.units.call_intelligence import sweep, worker
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
    await _calls_on(callback_url="https://crm.tenant-a.example/hooks")
    await _push()
    ctx["deliver"].crash_on = {1}

    with pytest.raises(RuntimeError):
        await process_call(ctx, "tenant-a", JOB)
    job = await _job()
    assert (job.status, job.delivery) == (JobStatus.DONE, DeliveryState.PENDING)

    await process_call(ctx, "tenant-a", JOB)

    job = await _job()
    assert (job.status, job.attempts) == (JobStatus.DONE, 1)
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


async def test_a_failed_job_with_its_call_failed_pending_sends_it_again(
    ctx: dict,
) -> None:
    """A run that died before call.failed went sends call.failed, not stage 1."""
    await _calls_on(callback_url="https://crm.tenant-a.example/hooks")
    await _push(audio_url_expires_at="2026-01-01T00:00:00+00:00")
    ctx["deliver"].crash_on = {1}
    with pytest.raises(RuntimeError):
        await process_call(ctx, "tenant-a", JOB)

    await process_call(ctx, "tenant-a", JOB)

    assert ctx["deliver"].events == [FAILED, FAILED]
    assert (await _job()).status is JobStatus.FAILED


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


# --- the run's own deadline (stuck jobs) --------------------------------------


class _HungTranscriber(FakeTranscriber):
    """Counts the call, then never answers."""

    async def transcribe(self, audio_path: Path, *, language_hint: str | None):
        await super().transcribe(audio_path, language_hint=language_hint)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.fixture
def short_deadline(monkeypatch) -> None:
    monkeypatch.setattr(worker, "deadline_seconds", lambda: 0.5)


async def _bounded(ctx: dict) -> None:
    """One run, failed rather than hung when no deadline fires."""
    await asyncio.wait_for(process_call(ctx, "tenant-a", JOB), 5)


async def test_a_hung_transcriber_ends_as_a_retry_then_dead_letter(
    ctx: dict, short_deadline: None, redis_fakes: RedisFakes
) -> None:
    """Two paid starts, each cut off, then dead_letter and call.failed."""
    await _calls_on()
    await _push()
    ctx["transcriber"] = _HungTranscriber()

    with pytest.raises(Retry):
        await _bounded(ctx)
    job = await _job()
    assert (job.status, job.reason, job.transcriptions) == (
        JobStatus.QUEUED,
        worker.DEADLINE,
        1,
    )

    await _bounded(ctx)

    job = await _job()
    assert (job.status, job.reason, job.attempts, job.transcriptions) == (
        JobStatus.DEAD_LETTER,
        worker.DEADLINE,
        2,
        2,
    )
    assert len(ctx["transcriber"].calls) == 2
    assert ctx["deliver"].events == [FAILED]


async def test_a_hung_download_is_retried_as_an_attempt(
    ctx: dict, short_deadline: None, source: _Source
) -> None:
    await _calls_on()
    await _push()

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(hang)) as http:
        ctx["http"] = http
        with pytest.raises(Retry):
            await _bounded(ctx)

    job = await _job()
    assert (job.status, job.reason, job.attempts) == (
        JobStatus.QUEUED,
        worker.DEADLINE,
        1,
    )
    assert ctx["transcriber"].calls == []


class _HungDeliveries(_Deliveries):
    async def __call__(self, job, event: str, config) -> bool:
        self.events.append(event)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("callback", "retried"),
    [("https://crm.tenant-a.example/hooks", True), (None, False)],
    ids=["callback-owed", "no-callback"],
)
async def test_a_hung_delivery_is_re_run_only_while_a_callback_is_owed(
    ctx: dict, short_deadline: None, callback: str | None, retried: bool
) -> None:
    await _calls_on(callback_url=callback)
    await _push(call_outcome="voicemail")
    ctx["deliver"] = _HungDeliveries()

    if retried:
        with pytest.raises(Retry):
            await _bounded(ctx)
    else:
        await _bounded(ctx)

    assert (await _job()).status is JobStatus.DONE


async def test_a_job_gone_when_the_deadline_passes_is_left_alone(
    ctx: dict, short_deadline: None, redis_fakes: RedisFakes
) -> None:
    await _calls_on()
    await _push(call_outcome="voicemail")

    class _Vanishing(_HungDeliveries):
        async def __call__(self, job, event: str, config) -> bool:
            await redis_fakes.jobs.delete(job_key("tenant-a", JOB))
            return await super().__call__(job, event, config)

    ctx["deliver"] = _Vanishing()
    await _bounded(ctx)
    assert await read_job("tenant-a", JOB) is None


async def test_a_timeout_that_is_not_the_deadline_is_an_error(ctx: dict) -> None:
    await _calls_on()
    await _push()

    class _TimesOut(FakeTranscriber):
        async def transcribe(self, audio_path: Path, *, language_hint: str | None):
            raise TimeoutError

    ctx["transcriber"] = _TimesOut()
    with pytest.raises(TimeoutError):
        await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).status is JobStatus.TRANSCRIBING


def test_the_deadline_is_the_job_timeout_less_its_margin(monkeypatch) -> None:
    assert worker.deadline_seconds() == 1800 - worker.DEADLINE_MARGIN_SECONDS
    monkeypatch.setenv("DODEAL_CALL_JOB_TIMEOUT_SECONDS", "60")
    get_settings.cache_clear()
    with pytest.raises(ConfigError):
        get_settings()


@pytest.mark.parametrize(("seconds", "refused"), [(119, True), (120, False)])
def test_a_job_timeout_under_120_is_refused(
    monkeypatch, seconds: int, refused: bool
) -> None:
    """A run gets at least 60 s before it stops itself."""
    monkeypatch.setenv("DODEAL_CALL_JOB_TIMEOUT_SECONDS", str(seconds))
    get_settings.cache_clear()
    if refused:
        with pytest.raises(ConfigError):
            get_settings()
    else:
        assert get_settings().call_job_timeout_seconds == 120


# --- stuck jobs: the sweep ------------------------------------------------------


def _stuck_later() -> datetime:
    """A moment past the sweep's threshold for a job moved now."""
    wait = get_settings().call_job_timeout_seconds + sweep.SWEEP_GRACE_SECONDS
    return datetime.now(UTC) + timedelta(seconds=wait + 5)


async def _swept_ids(redis_fakes: RedisFakes) -> list[str]:
    keys = await redis_fakes.queue.keys("arq:job:*sweep*")
    return sorted(key.decode() for key in keys)


async def _left_transcribing(ctx: dict) -> None:
    """A run that died mid-transcription and left the job with no worker."""
    ctx["transcriber"] = _DyingTranscriber()
    with pytest.raises(RuntimeError):
        await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).status is JobStatus.TRANSCRIBING


@pytest.mark.parametrize(
    ("deaths", "status"),
    [(1, JobStatus.DONE), (2, JobStatus.DEAD_LETTER)],
    ids=["retried", "dead-lettered"],
)
async def test_a_job_left_transcribing_is_swept_once_and_never_stays_stuck(
    ctx: dict, redis_fakes: RedisFakes, deaths: int, status: JobStatus
) -> None:
    await _calls_on()
    await _push()
    for _ in range(deaths):
        await _left_transcribing(ctx)

    assert await sweep.sweep(now=_stuck_later()) == 1
    assert await sweep.sweep(now=_stuck_later()) == 0
    assert await _swept_ids(redis_fakes) == [f"arq:job:tenant-a:{JOB}:sweep:1"]
    (queued,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)

    ctx["transcriber"] = FakeTranscriber()
    await process_call(ctx, *queued.args)

    job = await _job()
    assert job.status is status
    assert ctx["deliver"].events == [STAGE1 if status is JobStatus.DONE else FAILED]
    assert await sweep.sweep(now=_stuck_later()) == 0


async def test_a_job_moved_recently_or_waiting_on_purpose_is_not_swept(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    await _calls_on()
    await _push()
    assert await sweep.sweep(now=_stuck_later()) == 0
    await _left_transcribing(ctx)
    assert await sweep.sweep(now=datetime.now(UTC)) == 0
    assert await _swept_ids(redis_fakes) == []


async def test_a_sweep_with_the_queue_down_takes_its_mark_back(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    from dodeal_ai.core.errors import QueueUnavailable

    await _calls_on()
    await _push()
    await _left_transcribing(ctx)

    async def no_queue(*args: object, **kwargs: object) -> None:
        raise QueueUnavailable()

    with monkeypatch.context() as patched:
        patched.setattr(sweep, "enqueue_call", no_queue)
        assert await sweep.sweep(now=_stuck_later()) == 0

    assert await sweep.sweep(now=_stuck_later()) == 1
    assert await _swept_ids(redis_fakes) == [f"arq:job:tenant-a:{JOB}:sweep:2"]


def _later(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def test_a_queued_job_with_no_arq_entry_is_re_enqueued_after_an_hour(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    """The push lost its queue entry: the sweep puts the job back once."""
    await _calls_on()
    await _push()
    assert await sweep.sweep(now=_later(sweep.QUEUED_WAIT_SECONDS - 60)) == 0
    assert await sweep.sweep(now=_later(sweep.QUEUED_WAIT_SECONDS + 5)) == 1
    assert await sweep.sweep(now=_later(sweep.QUEUED_WAIT_SECONDS + 5)) == 0
    assert await _swept_ids(redis_fakes) == [f"arq:job:tenant-a:{JOB}:sweep:1"]
    (queued,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)

    await process_call(ctx, *queued.args)
    assert (await _job()).status is JobStatus.DONE


async def test_a_queued_job_arq_still_holds_is_left_waiting(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    """A backlog is not a stall: no second copy that could pay twice."""
    from dodeal_ai.units.call_intelligence.queues import enqueue_call

    await _calls_on()
    await _push()
    await enqueue_call("tenant-a", JOB, NORMAL_QUEUE)

    assert await sweep.sweep(now=_later(sweep.QUEUED_WAIT_SECONDS + 5)) == 0
    assert await _swept_ids(redis_fakes) == []
    job = await _job()
    assert job.sweeps == 0
    assert await redis_fakes.jobs.hget(job_key("tenant-a", JOB), "swept") is None


async def test_a_paused_job_is_swept_only_past_its_wake_up_and_off_arq(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    """Over budget it waits the window; its resume lost, the sweep wakes it."""
    await _calls_on()
    await _push()
    redis_fakes.cost.store["tokens:calls:tenant:tenant-a"] = 20_000_000
    await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).status is JobStatus.PAUSED_BUDGET
    window = get_settings().cost_window_seconds
    wake = await redis_fakes.jobs.zscore("call_jobs_active", f"tenant-a:{JOB}")
    assert abs(wake - _later(window).timestamp()) < 5

    past_wake = _later(window + sweep.PAUSED_WAIT_SECONDS + 5)
    assert await sweep.sweep(now=_later(sweep.QUEUED_WAIT_SECONDS + 5)) == 0
    assert await sweep.sweep(now=past_wake) == 0
    assert (await _job()).sweeps == 0

    await redis_fakes.queue.delete(f"arq:job:tenant-a:{JOB}:resume:1")
    assert await sweep.sweep(now=past_wake) == 1
    assert await _swept_ids(redis_fakes) == [f"arq:job:tenant-a:{JOB}:sweep:1"]


async def test_a_sweep_that_cannot_ask_arq_stops_and_takes_its_mark_back(
    ctx: dict, redis_fakes: RedisFakes, monkeypatch
) -> None:
    from dodeal_ai.core.errors import QueueUnavailable

    await _calls_on()
    await _push()

    async def no_queue(*args: object, **kwargs: object) -> bool:
        raise QueueUnavailable()

    monkeypatch.setattr(sweep, "on_the_queue", no_queue)
    assert await sweep.sweep(now=_later(sweep.QUEUED_WAIT_SECONDS + 5)) == 0
    assert await redis_fakes.jobs.hget(job_key("tenant-a", JOB), "swept") is None


async def test_arq_is_asked_under_every_id_the_job_can_have_had(
    redis_fakes: RedisFakes,
) -> None:
    from dodeal_ai.units.call_intelligence.queues import enqueue_call, on_the_queue

    assert not await on_the_queue("tenant-a", JOB, pauses=2, sweeps=2)
    await enqueue_call("tenant-a", JOB, NORMAL_QUEUE, sweep=2)
    assert await on_the_queue("tenant-a", JOB, pauses=2, sweeps=2)
    assert not await on_the_queue("tenant-a", JOB, pauses=2, sweeps=1)
    await redis_fakes.queue.set(f"arq:in-progress:tenant-a:{JOB}:resume:2", b"1")
    assert await on_the_queue("tenant-a", JOB, pauses=2, sweeps=0)


async def test_arq_unreachable_is_queue_unavailable(monkeypatch) -> None:
    import fakeredis

    from dodeal_ai.core.errors import QueueUnavailable
    from dodeal_ai.units.call_intelligence import queues

    server = fakeredis.FakeServer()
    server.connected = False
    down = fakeredis.FakeAsyncRedis(server=server)
    monkeypatch.setattr(queues, "get_queue_client", lambda: down)
    with pytest.raises(QueueUnavailable):
        await queues.on_the_queue("tenant-a", JOB, pauses=0, sweeps=0)


async def test_the_cron_task_sweeps_now() -> None:
    assert await sweep.sweep_stuck_jobs({}) == 0


def test_only_the_normal_queues_worker_sweeps_every_five_minutes() -> None:
    crons = {
        name: calls_worker.worker_settings(queue)["cron_jobs"]
        for name, queue in calls_worker.QUEUES.items()
    }
    (job,) = crons["normal"]
    assert (job.name, job.minute) == (
        "cron:sweep_stuck_jobs",
        {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
    )
    assert crons["priority"] == crons["overnight"] == []


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


def _llm_env(monkeypatch, **more: str) -> None:
    """A provider and pinned model, as a deployment sets them for the service."""
    for name, value in {
        "PROVIDER": "groq",
        "MODEL": "model-pinned-2026-01-01",
        "API_KEY": "test-only-key",
        **more,
    }.items():
        monkeypatch.setenv(f"DODEAL_LLM_{name}", value)
    get_settings.cache_clear()


async def test_the_worker_settings_carry_one_more_arq_try_than_the_job(
    monkeypatch,
) -> None:
    """The demo flag on, a handed-in transcriber is the worker's."""
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    _llm_env(monkeypatch)
    fake = FakeTranscriber()
    built = calls_worker.worker_settings(NORMAL_QUEUE, transcriber=fake)
    assert [(f.name, f.max_tries) for f in built["functions"]] == [
        ("process_call", 3),
        ("deliver_callback", 2),
    ]
    assert built["queue_name"] == NORMAL_QUEUE
    assert built["job_timeout"] == 1800

    ctx: dict[str, Any] = {}
    await built["on_startup"](ctx)
    assert ctx["transcriber"] is fake
    assert callable(ctx["deliver"])
    assert isinstance(ctx["http"], httpx.AsyncClient)
    assert ctx["http"].follow_redirects is False
    assert isinstance(ctx["llm"], OpenAICompatibleClient)
    await built["on_shutdown"](ctx)
    assert ctx["http"].is_closed and ctx["llm_http"].is_closed
    await built["on_shutdown"]({})


async def test_a_worker_with_no_llm_client_refuses_to_start(monkeypatch) -> None:
    """No model, no start: never a transcript paid for with nothing to read it."""
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    get_settings.cache_clear()
    built = calls_worker.worker_settings(NORMAL_QUEUE, transcriber=FakeTranscriber())
    ctx: dict[str, Any] = {}
    with pytest.raises(ConfigError, match="^llm_not_configured$"):
        await built["on_startup"](ctx)
    assert "llm" not in ctx and "http" not in ctx


async def test_the_workers_client_runs_the_services_profile_sweep(
    monkeypatch,
) -> None:
    """The same factory as main.py: a profile naming another vendor refuses."""
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    profile = '{"unit_b.extract":{"provider":"openai","model":"m"}}'
    _llm_env(monkeypatch, PROFILES=profile)
    built = calls_worker.worker_settings(NORMAL_QUEUE, transcriber=FakeTranscriber())
    with pytest.raises(ConfigError, match="^llm_profile_provider_mismatch"):
        await built["on_startup"]({})


def test_each_queue_carries_the_configured_job_timeout(monkeypatch) -> None:
    """CALL_JOB_TIMEOUT_SECONDS is every call queue's arq job timeout."""
    monkeypatch.setenv("DODEAL_CALL_JOB_TIMEOUT_SECONDS", "2400")
    get_settings.cache_clear()
    for queue in calls_worker.QUEUES.values():
        assert calls_worker.worker_settings(queue)["job_timeout"] == 2400


async def test_the_fake_with_the_demo_flag_off_refuses_to_start() -> None:
    """No real call is ever answered with the fake's invented words."""
    built = calls_worker.worker_settings(NORMAL_QUEUE, transcriber=FakeTranscriber())
    ctx: dict[str, Any] = {}
    with pytest.raises(ConfigError, match="^fake_transcriber_needs_demo$"):
        await built["on_startup"](ctx)
    assert "transcriber" not in ctx


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


def test_the_queue_redis_is_derived_from_the_configured_url(monkeypatch) -> None:
    """Every field of arq's settings comes from DODEAL_REDIS_QUEUE_URL."""
    monkeypatch.setenv("DODEAL_REDIS_QUEUE_URL", "redis://queue.example:6380/7")
    get_settings.cache_clear()
    built = calls_worker.worker_settings(NORMAL_QUEUE)["redis_settings"]
    assert (built.host, built.port, built.database) == ("queue.example", 6380, 7)


def test_the_call_workers_are_the_only_worker_entry_point() -> None:
    """The empty runner skeleton is gone and nothing can import it."""
    import importlib.util

    assert importlib.util.find_spec("dodeal_ai.workers.runner") is None


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
