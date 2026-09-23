"""The job store's scripts on a real server (register item 50): one job per
call under a real concurrent burst, and terminal is final with both keys
expiring. Keys are the production spellings under the per-test prefix."""

from __future__ import annotations

import asyncio

import pytest
from redis import asyncio as redis_async

from dodeal_ai.core.jobs import (
    _CLAIM_SCRIPT,
    _CREATE_SCRIPT,
    _TERMINAL_ARGS,
    _TRANSITION_SCRIPT,
    call_index_key,
    job_key,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_TENANT = "tenant-a"
_CALL = 7
_TTL = 259_200


async def _create(
    client: redis_async.Redis, prefix: str, job_id: str
) -> tuple[int, str]:
    made, existing = await client.eval(
        _CREATE_SCRIPT,
        2,
        prefix + call_index_key(_TENANT, _CALL),
        prefix + job_key(_TENANT, job_id),
        job_id,
        "status",
        "queued",
        "attempts",
        "0",
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
    job, index = (
        key_prefix + job_key(_TENANT, "job-1"),
        key_prefix + call_index_key(_TENANT, _CALL),
    )

    moved = await real_redis.eval(
        _TRANSITION_SCRIPT, 2, job, index, "done", "t", "", _TTL, "1", *_TERMINAL_ARGS
    )
    again = await real_redis.eval(
        _TRANSITION_SCRIPT, 2, job, index, "failed", "t", "", _TTL, "1", *_TERMINAL_ARGS
    )
    claim = await real_redis.eval(
        _CLAIM_SCRIPT, 1, job, 2, "t", "downloading", *_TERMINAL_ARGS
    )

    assert (int(moved), int(again), [int(x) for x in claim]) == (1, -1, [-1, 0])
    assert await real_redis.hget(job, "status") == "done"
    for key in (job, index):
        assert 0 < await real_redis.ttl(key) <= _TTL
