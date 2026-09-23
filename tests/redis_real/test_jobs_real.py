"""The job store's scripts on a real server (register item 50): one job per
call under a real concurrent burst, terminal is final with both keys
expiring, a running job expires a record TTL after each move, and a stuck job
is marked once. Keys are the production spellings under the per-test prefix."""

from __future__ import annotations

import asyncio

import pytest
from redis import asyncio as redis_async

from dodeal_ai.core.jobs import (
    _CLAIM_SCRIPT,
    _CREATE_SCRIPT,
    _SWEEP_SCRIPT,
    _TERMINAL_ARGS,
    _TRANSITION_SCRIPT,
    ACTIVE_JOBS_KEY,
    active_member,
    call_index_key,
    job_key,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_TENANT = "tenant-a"
_CALL = 7
_TTL = 259_200
_RECORD_TTL = 604_800
_SCORE = 1_790_000_000.0


def _keys(prefix: str, job_id: str) -> tuple[str, str, str]:
    return (
        prefix + job_key(_TENANT, job_id),
        prefix + call_index_key(_TENANT, _CALL),
        prefix + ACTIVE_JOBS_KEY,
    )


def _touch(job_id: str) -> tuple[object, ...]:
    return _RECORD_TTL, _SCORE, active_member(_TENANT, job_id)


async def _create(
    client: redis_async.Redis, prefix: str, job_id: str
) -> tuple[int, str]:
    made, existing = await client.eval(
        _CREATE_SCRIPT,
        3,
        *_keys(prefix, job_id),
        job_id,
        *_touch(job_id),
        "status",
        "queued",
        "attempts",
        "0",
        "updated_at",
        "t0",
        "queue",
        "arq:calls:normal",
    )
    return int(made), str(existing)


async def test_a_burst_of_pushes_for_one_call_makes_one_job(
    real_redis: redis_async.Redis, key_prefix: str
) -> None:
    """Twenty concurrent pushes, on the pool's own connections: one is made,
    all twenty are told its job_id, and exactly one job hash exists."""
    results = await asyncio.gather(
        *(_create(real_redis, key_prefix, f"job-{n}") for n in range(20))
    )

    made = [job_id for flag, job_id in results if flag == 1]
    assert len(made) == 1
    assert {job_id for _, job_id in results} == set(made)
    jobs = [key async for key in real_redis.scan_iter(match=f"{key_prefix}call_job:*")]
    assert jobs == [key_prefix + job_key(_TENANT, made[0])]


async def test_a_terminal_move_is_final_and_expires_both_keys(
    real_redis: redis_async.Redis, key_prefix: str
) -> None:
    await _create(real_redis, key_prefix, "job-1")
    keys = _keys(key_prefix, "job-1")
    job, index, active = keys

    async def _move(status: str) -> int:
        return await real_redis.eval(
            _TRANSITION_SCRIPT,
            3,
            *keys,
            status,
            "t",
            "",
            _TTL,
            "1",
            "",
            *_touch("job-1"),
            *_TERMINAL_ARGS,
        )

    moved, again = await _move("done"), await _move("failed")
    claim = await real_redis.eval(
        _CLAIM_SCRIPT,
        3,
        *keys,
        2,
        "t",
        "downloading",
        *_touch("job-1"),
        *_TERMINAL_ARGS,
    )

    assert (int(moved), int(again), [int(x) for x in claim]) == (1, -1, [-1, 0])
    assert await real_redis.hget(job, "status") == "done"
    for key in (job, index):
        assert 0 < await real_redis.ttl(key) <= _TTL
    assert await real_redis.zscore(active, active_member(_TENANT, "job-1")) is None


async def test_a_running_job_expires_and_is_swept_once_per_stall(
    real_redis: redis_async.Redis, key_prefix: str
) -> None:
    """The push gives both keys the record TTL and a score; a stuck job is
    marked once for its last transition, then not again."""
    await _create(real_redis, key_prefix, "job-1")
    job, index, active = _keys(key_prefix, "job-1")
    member = active_member(_TENANT, "job-1")
    for key in (job, index):
        assert _TTL < await real_redis.ttl(key) <= _RECORD_TTL
    assert await real_redis.zscore(active, member) == _SCORE

    async def _sweep() -> list[object]:
        return await real_redis.eval(
            _SWEEP_SCRIPT, 2, job, active, member, _SCORE, "queued", 0
        )

    first, second = await _sweep(), await _sweep()
    assert first == [1, 1, "arq:calls:normal", "queued", "0"]
    assert second == [-1]
