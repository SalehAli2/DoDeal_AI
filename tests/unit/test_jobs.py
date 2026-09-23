"""Unit B's job store in db3 (register item 50), on fakeredis so the scripts run
as Lua: one job per (tenant, call_id), terminal is final, the result is held
apart, and a store that cannot answer fails closed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest

from dodeal_ai.core import jobs
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.jobs import (
    ACTIVE_JOBS_KEY,
    DeliveryState,
    JobStatus,
    JobStoreUnavailable,
    Stage2State,
    Swept,
    call_index_key,
    claim_attempt,
    clear_work,
    create_job,
    job_key,
    mark_swept,
    note_wake,
    pause,
    read_job,
    read_result,
    read_work,
    result_key,
    settle_delivery,
    settle_stage2,
    stale_jobs,
    start_pass,
    start_stage2_pass,
    start_transcription,
    store_result,
    store_work,
    transition,
    unmark_swept,
)
from tests.conftest import RedisFakes

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
LINK = "https://audio.tenant-a.example/call-7.wav?sig=SECRET"
TTL = 259_200


async def _create(
    job_id: str = "job-1", call_id: int = 7, tenant: str = "tenant-a"
) -> tuple[str, bool]:
    return await create_job(
        tenant,
        call_id,
        job_id=job_id,
        request_id="req-1",
        queue="calls:normal",
        metadata={"audio_url": LINK, "duration_seconds": 95},
        now=NOW,
    )


async def _job(job_id: str = "job-1", tenant: str = "tenant-a") -> jobs.Job:
    job = await read_job(tenant, job_id)
    assert job is not None
    return job


async def test_a_created_job_reads_back_queued_with_its_fields() -> None:
    assert await _create() == ("job-1", True)
    job = await _job()
    assert (job.status, job.attempts, job.pauses, job.reason, job.queue) == (
        JobStatus.QUEUED,
        0,
        0,
        None,
        "calls:normal",
    )
    assert (job.call_id, job.request_id, job.created_at, job.finished_at) == (
        7,
        "req-1",
        NOW.isoformat(),
        None,
    )
    assert job.metadata["audio_url"] == LINK
    assert not job.terminal
    assert "SECRET" not in repr(job)


async def test_two_concurrent_pushes_of_one_call_make_one_job(
    redis_fakes: RedisFakes,
) -> None:
    """The guard: both pushes are told the same job_id, and one job exists."""
    first, second = await asyncio.gather(_create("job-a"), _create("job-b"))

    assert first[0] == second[0]
    assert sorted([first[1], second[1]]) == [False, True]
    keys = await redis_fakes.jobs.keys("call_job:*")
    assert keys == [job_key("tenant-a", first[0])]


async def test_a_second_push_later_is_told_the_first_job() -> None:
    await _create("job-1")
    assert await _create("job-2") == ("job-1", False)
    assert await read_job("tenant-a", "job-2") is None


async def test_the_same_call_id_in_two_tenants_is_two_jobs() -> None:
    """The tenant is in every key, so neither tenant can read the other's."""
    assert await _create("job-1", tenant="tenant-a") == ("job-1", True)
    assert await _create("job-2", tenant="tenant-b") == ("job-2", True)
    assert await read_job("tenant-b", "job-1") is None


RECORD_TTL = 604_800
LATER = NOW + timedelta(minutes=5)


async def _record_ttls(redis_fakes: RedisFakes) -> list[int]:
    return [
        await redis_fakes.jobs.ttl(key)
        for key in (job_key("tenant-a", "job-1"), call_index_key("tenant-a", 7))
    ]


async def _score(redis_fakes: RedisFakes) -> float | None:
    return await redis_fakes.jobs.zscore(ACTIVE_JOBS_KEY, "tenant-a:job-1")


async def test_a_new_job_expires_a_record_ttl_after_its_push(
    redis_fakes: RedisFakes,
) -> None:
    """Job and index both expire; the job is active from its push."""
    await _create()
    assert all(0 < ttl <= RECORD_TTL for ttl in await _record_ttls(redis_fakes))
    assert await _score(redis_fakes) == NOW.timestamp()


@pytest.mark.parametrize(
    "move",
    ["transition", "claim", "pause", "transcription"],
)
async def test_every_running_move_renews_the_record_ttl_and_the_score(
    redis_fakes: RedisFakes, move: str
) -> None:
    await _create()
    for key in (job_key("tenant-a", "job-1"), call_index_key("tenant-a", 7)):
        await redis_fakes.jobs.persist(key)
    job = await _job()
    moves = {
        "transition": lambda: transition(
            job, JobStatus.DELIVERING, now=LATER, ttl_seconds=TTL
        ),
        "claim": lambda: claim_attempt(
            job, max_tries=2, now=LATER, status=JobStatus.DOWNLOADING
        ),
        "pause": lambda: pause(job, now=LATER, reason="token_budget_exceeded"),
        "transcription": lambda: start_transcription(job, now=LATER),
    }

    assert await moves[move]()

    assert all(0 < ttl <= RECORD_TTL for ttl in await _record_ttls(redis_fakes))
    assert await _score(redis_fakes) == LATER.timestamp()


async def test_a_record_ttl_is_the_configured_one(
    redis_fakes: RedisFakes, monkeypatch
) -> None:
    monkeypatch.setenv("DODEAL_CALL_JOB_RECORD_TTL_SECONDS", "3600")
    get_settings.cache_clear()
    await _create()
    assert all(3500 < ttl <= 3600 for ttl in await _record_ttls(redis_fakes))


async def test_a_terminal_transition_is_final_and_expires_job_and_index(
    redis_fakes: RedisFakes,
) -> None:
    await _create()
    job = await _job()
    assert await transition(
        job, JobStatus.FAILED, now=NOW, reason="audio_link_expired", ttl_seconds=TTL
    )

    failed = await _job()
    assert (failed.status, failed.reason, failed.finished_at) == (
        JobStatus.FAILED,
        "audio_link_expired",
        NOW.isoformat(),
    )
    assert failed.terminal
    for key in (job_key("tenant-a", "job-1"), call_index_key("tenant-a", 7)):
        assert 0 < await redis_fakes.jobs.ttl(key) <= TTL
    assert await _score(redis_fakes) is None
    assert not await transition(job, JobStatus.DONE, now=NOW, ttl_seconds=TTL)
    assert (
        await claim_attempt(job, max_tries=2, now=NOW, status=JobStatus.DOWNLOADING)
        is None
    )
    assert await pause(job, now=NOW, reason="token_budget_exceeded") is None
    assert (await _job()).status is JobStatus.FAILED


async def test_a_missing_job_moves_nothing() -> None:
    await _create()
    job = await _job()
    await jobs.get_jobs_client().delete(job_key("tenant-a", "job-1"))
    assert not await transition(job, JobStatus.DONE, now=NOW, ttl_seconds=TTL)
    assert (
        await claim_attempt(job, max_tries=2, now=NOW, status=JobStatus.DOWNLOADING)
        is None
    )
    assert await pause(job, now=NOW, reason="x") is None


async def test_a_claim_counts_attempts_up_to_the_cap() -> None:
    """Two claims, then 0: the caller dead-letters and nothing moves."""
    await _create()
    job = await _job()
    await transition(job, JobStatus.DOWNLOADING, now=NOW, reason="r", ttl_seconds=TTL)

    claims = [
        await claim_attempt(job, max_tries=2, now=NOW, status=JobStatus.DOWNLOADING)
        for _ in range(3)
    ]

    assert claims == [1, 2, 0]
    claimed = await _job()
    assert (claimed.attempts, claimed.reason) == (2, None)


async def test_a_delivery_moves_with_a_transition_then_only_out_of_pending(
    redis_fakes: RedisFakes,
) -> None:
    """Settled once, never again, and the status is never touched by it."""
    await _create()
    job = await _job()
    assert job.delivery is None
    assert not await settle_delivery(job, DeliveryState.DELIVERED, now=NOW)
    await transition(
        job,
        JobStatus.DONE,
        now=NOW,
        ttl_seconds=TTL,
        delivery=DeliveryState.PENDING,
    )
    assert (await _job()).delivery is DeliveryState.PENDING
    assert await settle_delivery(job, DeliveryState.DELIVERY_FAILED, now=NOW)
    assert not await settle_delivery(job, DeliveryState.DELIVERED, now=NOW)
    settled = await _job()
    assert (settled.status, settled.delivery) == (
        JobStatus.DONE,
        DeliveryState.DELIVERY_FAILED,
    )
    assert 0 < await redis_fakes.jobs.ttl(job_key("tenant-a", "job-1")) <= TTL
    await redis_fakes.jobs.delete(job_key("tenant-a", "job-1"))
    assert not await settle_delivery(job, DeliveryState.DELIVERED, now=NOW)


async def test_a_transcription_is_counted_as_it_starts() -> None:
    """Each start is one more paid call, counted in the same script."""
    await _create()
    job = await _job()
    assert job.transcriptions == 0
    assert await start_transcription(job, now=NOW) == 1
    assert await start_transcription(job, now=NOW) == 2
    started = await _job()
    assert (started.status, started.transcriptions, started.reason) == (
        JobStatus.TRANSCRIBING,
        2,
        None,
    )


async def test_a_terminal_or_missing_job_starts_no_transcription(
    redis_fakes: RedisFakes,
) -> None:
    """Terminal is final and a gone job is not revived by the count."""
    await _create()
    job = await _job()
    await transition(job, JobStatus.FAILED, now=NOW, ttl_seconds=TTL)
    assert await start_transcription(job, now=NOW) is None
    await redis_fakes.jobs.delete(job_key("tenant-a", "job-1"))
    assert await start_transcription(job, now=NOW) is None


async def test_a_job_stored_before_the_count_reads_as_none_started(
    redis_fakes: RedisFakes,
) -> None:
    """A hash with no transcriptions field reads as zero, never a KeyError."""
    await _create()
    await redis_fakes.jobs.hdel(job_key("tenant-a", "job-1"), "transcriptions")
    assert (await _job()).transcriptions == 0


async def test_a_pause_counts_and_leaves_attempts_alone() -> None:
    """Every pause counts; only an outage counts as an outage too."""
    await _create()
    job = await _job()
    assert await pause(job, now=NOW, reason="token_budget_exceeded") == (1, 0)
    assert await pause(job, now=NOW, reason="cost_store_unavailable", outage=True) == (
        2,
        1,
    )
    paused = await _job()
    assert (
        paused.status,
        paused.reason,
        paused.pauses,
        paused.outages,
        paused.attempts,
    ) == (JobStatus.PAUSED_BUDGET, "cost_store_unavailable", 2, 1, 0)


async def test_a_pause_after_a_claim_gives_the_attempt_back() -> None:
    """A claimed attempt is returned, and a count at zero never goes below."""
    await _create()
    job = await _job()
    assert (
        await claim_attempt(job, max_tries=2, now=NOW, status=JobStatus.DOWNLOADING)
        == 1
    )
    await pause(job, now=NOW, reason="cost_store_unavailable", claimed=True)
    assert (await _job()).attempts == 0
    await pause(job, now=NOW, reason="cost_store_unavailable", claimed=True)
    assert (await _job()).attempts == 0


async def test_a_job_stored_before_the_outage_count_reads_as_none(
    redis_fakes: RedisFakes,
) -> None:
    """A hash with no outages field reads as zero, never a KeyError."""
    await _create()
    await redis_fakes.jobs.hdel(job_key("tenant-a", "job-1"), "outages")
    assert (await _job()).outages == 0


async def test_the_result_is_held_apart_for_its_ttl(redis_fakes: RedisFakes) -> None:
    assert await read_result("tenant-a", "job-1") is None
    await store_result("tenant-a", "job-1", {"segments": []}, ttl_seconds=TTL)
    assert await read_result("tenant-a", "job-1") == {"segments": []}
    assert await read_result("tenant-b", "job-1") is None
    assert 0 < await redis_fakes.jobs.ttl(result_key("tenant-a", "job-1")) <= TTL


@pytest.fixture
def dead_store(monkeypatch) -> fakeredis.FakeAsyncRedis:
    """A db3 that refuses every command the way a dead server does."""
    server = fakeredis.FakeServer()
    server.connected = False
    client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    monkeypatch.setattr(jobs, "get_jobs_client", lambda: client)
    return client


async def test_every_operation_fails_closed_on_a_dead_store(dead_store) -> None:
    job = jobs.Job(
        tenant="tenant-a",
        job_id="job-1",
        call_id=7,
        status=JobStatus.QUEUED,
        attempts=0,
        transcriptions=0,
        pauses=0,
        outages=0,
        sweeps=0,
        passes={},
        request_id="req-1",
        reason=None,
        delivery=None,
        stage2=None,
        stage2_reason=None,
        queue="calls:normal",
        created_at="t",
        updated_at="t",
        finished_at=None,
        metadata={},
    )
    operations = [
        _create(),
        read_job("tenant-a", "job-1"),
        transition(job, JobStatus.DONE, now=NOW, ttl_seconds=TTL),
        claim_attempt(job, max_tries=2, now=NOW, status=JobStatus.DOWNLOADING),
        pause(job, now=NOW, reason="x"),
        start_transcription(job, now=NOW),
        settle_delivery(job, DeliveryState.DELIVERED, now=NOW),
        store_result("tenant-a", "job-1", {}, ttl_seconds=TTL),
        read_result("tenant-a", "job-1"),
        stale_jobs(NOW, limit=10),
        start_pass(job, "extract", now=NOW),
        store_work("tenant-a", "job-1", "transcript", {}, ttl_seconds=TTL),
        read_work("tenant-a", "job-1"),
        clear_work("tenant-a", "job-1"),
        mark_swept("tenant-a", "job-1", now=NOW, waits={JobStatus.QUEUED: 0}),
        unmark_swept("tenant-a", "job-1"),
        note_wake(job, wake_at=NOW),
        settle_stage2(job, Stage2State.DONE, now=NOW),
        start_stage2_pass(job, "objections", now=NOW),
    ]
    for operation in operations:
        with pytest.raises(JobStoreUnavailable) as caught:
            await operation
        assert str(caught.value) == "job_store_unavailable"
        assert caught.value.__cause__ is None


def test_a_hash_value_is_read_as_text_whichever_way_the_client_decodes() -> None:
    assert (jobs._text(b"queued"), jobs._text("queued")) == ("queued", "queued")


# --- the active set and the sweep's mark ---------------------------------------

STUCK = {JobStatus.TRANSCRIBING: 0}


def _swept(sweeps: int, status: JobStatus = JobStatus.TRANSCRIBING) -> Swept:
    return Swept(sweeps=sweeps, queue="calls:normal", status=status, pauses=0)


async def test_stale_jobs_are_read_oldest_first_up_to_the_limit() -> None:
    for n, minutes in ((1, 30), (2, 50), (3, 5)):
        await create_job(
            "tenant-a",
            n,
            job_id=f"job-{n}",
            request_id="req-1",
            queue="calls:normal",
            metadata={},
            now=NOW - timedelta(minutes=minutes),
        )

    stale = await stale_jobs(NOW - timedelta(minutes=10), limit=10)
    assert stale == [("tenant-a", "job-2"), ("tenant-a", "job-1")]
    assert await stale_jobs(NOW, limit=1) == [("tenant-a", "job-2")]


async def test_a_stuck_job_is_marked_once_per_transition() -> None:
    await _create()
    assert await start_transcription(await _job(), now=NOW) == 1

    assert await mark_swept("tenant-a", "job-1", now=NOW, waits=STUCK) == _swept(1)
    assert await mark_swept("tenant-a", "job-1", now=NOW, waits=STUCK) is None

    await unmark_swept("tenant-a", "job-1")
    assert await mark_swept("tenant-a", "job-1", now=NOW, waits=STUCK) == _swept(2)
    await transition(await _job(), JobStatus.TRANSCRIBING, now=LATER, ttl_seconds=TTL)
    assert await mark_swept("tenant-a", "job-1", now=LATER, waits=STUCK) == _swept(3)
    assert (await _job()).sweeps == 3


async def test_a_job_in_another_status_is_not_marked() -> None:
    await _create()
    assert await mark_swept("tenant-a", "job-1", now=NOW, waits=STUCK) is None


async def test_a_job_is_marked_only_once_past_its_wait() -> None:
    """The wait runs from the job's score in the active set."""
    await _create()
    waits = {JobStatus.QUEUED: 60}
    early = NOW + timedelta(seconds=59)
    assert await mark_swept("tenant-a", "job-1", now=early, waits=waits) is None
    marked = await mark_swept(
        "tenant-a", "job-1", now=NOW + timedelta(seconds=60), waits=waits
    )
    assert marked == _swept(1, JobStatus.QUEUED)


async def test_taking_a_sweep_back_uncounts_it_and_a_gone_job_is_not_written(
    redis_fakes: RedisFakes,
) -> None:
    await _create()
    waits = {JobStatus.QUEUED: 0}
    assert await mark_swept("tenant-a", "job-1", now=NOW, waits=waits) is not None
    await unmark_swept("tenant-a", "job-1", uncount=True)
    assert (await _job()).sweeps == 0
    assert await mark_swept("tenant-a", "job-1", now=NOW, waits=waits) == _swept(
        1, JobStatus.QUEUED
    )
    await unmark_swept("tenant-a", "job-2", uncount=True)
    assert not await redis_fakes.jobs.exists(job_key("tenant-a", "job-2"))


async def test_a_paused_job_is_scored_at_its_wake_up(redis_fakes: RedisFakes) -> None:
    await _create()
    job = await _job()
    wake = NOW + timedelta(hours=24)
    await pause(job, now=NOW, reason="token_budget_exceeded")
    assert await note_wake(job, wake_at=wake) is True
    assert await _score(redis_fakes) == wake.timestamp()
    marked = await mark_swept(
        "tenant-a", "job-1", now=wake, waits={JobStatus.PAUSED_BUDGET: 0}
    )
    assert marked == Swept(
        sweeps=1, queue="calls:normal", status=JobStatus.PAUSED_BUDGET, pauses=1
    )

    await transition(job, JobStatus.DOWNLOADING, now=LATER, ttl_seconds=TTL)
    assert await note_wake(job, wake_at=wake) is False
    assert await _score(redis_fakes) == LATER.timestamp()


async def test_a_job_gone_leaves_the_active_set(redis_fakes: RedisFakes) -> None:
    await _create()
    await redis_fakes.jobs.delete(job_key("tenant-a", "job-1"))

    assert await mark_swept("tenant-a", "job-1", now=NOW, waits=STUCK) is None
    assert await _score(redis_fakes) is None


# --- stage 2's state ---------------------------------------------------------------


async def test_stage2_is_set_with_done_and_settles_once_out_of_pending() -> None:
    await _create()
    job = await _job()
    assert await transition(
        job, JobStatus.DONE, now=NOW, ttl_seconds=TTL, stage2=Stage2State.PENDING
    )
    assert (await _job()).stage2 is Stage2State.PENDING

    assert await settle_stage2(job, Stage2State.FAILED, now=LATER, reason="x")
    settled = await _job()
    assert (settled.status, settled.stage2, settled.stage2_reason) == (
        JobStatus.DONE,
        Stage2State.FAILED,
        "x",
    )
    assert not await settle_stage2(job, Stage2State.DONE, now=LATER)
    await jobs.get_jobs_client().delete(job_key("tenant-a", "job-1"))
    assert not await settle_stage2(job, Stage2State.DONE, now=LATER)


async def test_a_stage2_pass_is_counted_on_a_done_job_only_while_pending(
    redis_fakes: RedisFakes,
) -> None:
    await _create()
    job = await _job()
    assert await start_stage2_pass(job, "objections", now=NOW) is None
    await transition(
        job, JobStatus.DONE, now=NOW, ttl_seconds=TTL, stage2=Stage2State.PENDING
    )
    assert await start_stage2_pass(job, "objections", now=NOW) == 1
    assert await start_stage2_pass(job, "objections", now=NOW) == 2
    assert (await _job()).passes == {"objections": 2}
    assert await _score(redis_fakes) is None

    await settle_stage2(job, Stage2State.DONE, now=NOW)
    assert await start_stage2_pass(job, "objections", now=NOW) is None
    await jobs.get_jobs_client().delete(job_key("tenant-a", "job-1"))
    assert await start_stage2_pass(job, "objections", now=NOW) is None


async def test_a_move_without_a_stage2_state_leaves_it_alone() -> None:
    await _create()
    job = await _job()
    await transition(job, JobStatus.DOWNLOADING, now=NOW, ttl_seconds=TTL)
    assert (await _job()).stage2 is None


# --- stage 1's work and the pass counts ------------------------------------------


async def test_a_pass_start_is_counted_and_touches_the_job(
    redis_fakes: RedisFakes,
) -> None:
    await _create()
    job = await _job()
    assert await start_pass(job, "extract", now=LATER) == 1
    assert await start_pass(job, "extract", now=LATER) == 2
    assert await start_pass(job, "prose", now=LATER) == 1

    moved = await _job()
    assert moved.passes == {"extract": 2, "prose": 1}
    assert (moved.status, moved.updated_at) == (JobStatus.QUEUED, LATER.isoformat())
    assert await _score(redis_fakes) == LATER.timestamp()


async def test_a_terminal_or_missing_job_starts_no_pass() -> None:
    await _create()
    job = await _job()
    await transition(job, JobStatus.FAILED, now=NOW, ttl_seconds=TTL)
    assert await start_pass(job, "extract", now=NOW) is None
    await _create("job-2", call_id=8)
    gone = await _job("job-2")
    await jobs.get_jobs_client().delete(job_key("tenant-a", "job-2"))
    assert await start_pass(gone, "extract", now=NOW) is None


async def test_the_work_is_kept_for_its_ttl_then_cleared(
    redis_fakes: RedisFakes,
) -> None:
    await store_work("tenant-a", "job-1", "transcript", {"a": 1}, ttl_seconds=TTL)
    await store_work("tenant-a", "job-1", "extract", {"failed": "x"}, ttl_seconds=TTL)

    assert await read_work("tenant-a", "job-1") == {
        "transcript": {"a": 1},
        "extract": {"failed": "x"},
    }
    assert 0 < await redis_fakes.jobs.ttl(jobs.work_key("tenant-a", "job-1")) <= TTL
    await clear_work("tenant-a", "job-1")
    assert await read_work("tenant-a", "job-1") == {}
