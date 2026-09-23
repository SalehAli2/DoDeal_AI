"""Unit B's job store in db3 (register item 50), on fakeredis so the scripts run
as Lua: one job per (tenant, call_id), terminal is final, the result is held
apart, and a store that cannot answer fails closed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import fakeredis
import pytest

from dodeal_ai.core import jobs
from dodeal_ai.core.jobs import (
    JobStatus,
    JobStoreUnavailable,
    call_index_key,
    claim_attempt,
    create_job,
    job_key,
    pause,
    read_job,
    read_result,
    result_key,
    start_transcription,
    store_result,
    transition,
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


async def test_a_running_transition_holds_no_ttl(redis_fakes: RedisFakes) -> None:
    await _create()
    assert await transition(
        await _job(), JobStatus.DELIVERING, now=NOW, ttl_seconds=TTL
    )
    assert (await _job()).status is JobStatus.DELIVERING
    assert await redis_fakes.jobs.ttl(job_key("tenant-a", "job-1")) == -1


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
    await _create()
    job = await _job()
    assert await pause(job, now=NOW, reason="token_budget_exceeded") == 1
    assert await pause(job, now=NOW, reason="token_store_unavailable") == 2
    paused = await _job()
    assert (paused.status, paused.reason, paused.pauses, paused.attempts) == (
        JobStatus.PAUSED_BUDGET,
        "token_store_unavailable",
        2,
        0,
    )


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
        request_id="req-1",
        reason=None,
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
        store_result("tenant-a", "job-1", {}, ttl_seconds=TTL),
        read_result("tenant-a", "job-1"),
    ]
    for operation in operations:
        with pytest.raises(JobStoreUnavailable) as caught:
            await operation
        assert str(caught.value) == "job_store_unavailable"
        assert caught.value.__cause__ is None


def test_a_hash_value_is_read_as_text_whichever_way_the_client_decodes() -> None:
    assert (jobs._text(b"queued"), jobs._text("queued")) == ("queued", "queued")
