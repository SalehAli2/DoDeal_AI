"""The WhatsApp route's Redis spellings (core/jobs.py) on a live server: the
claim that lets one request pay, the TTL read that bounds the hold, and the
one MGET the status route reads every language with."""

from __future__ import annotations

import asyncio

import pytest
from redis import asyncio as redis_async

from dodeal_ai.core.jobs import whatsapp_claim_key, whatsapp_key

pytestmark = [pytest.mark.redis_real, pytest.mark.asyncio(loop_scope="session")]

_TTL = 30


async def test_ten_claims_on_one_language_have_exactly_one_winner(
    real_redis: redis_async.Redis, key_prefix: str
) -> None:
    """claim_whatsapp's SET NX EX: one request pays, the other nine get 409."""
    key = key_prefix + whatsapp_claim_key("tenant-a", "job-1", "ru")
    claims = await asyncio.gather(
        *(real_redis.set(key, "1", nx=True, ex=_TTL) for _ in range(10))
    )
    assert [bool(claim) for claim in claims].count(True) == 1
    assert 0 < await real_redis.ttl(key) <= _TTL


async def test_ttl_is_negative_for_a_gone_or_unbounded_result(
    real_redis: redis_async.Redis, key_prefix: str
) -> None:
    """result_seconds_left holds nothing unless the answer is positive."""
    key = key_prefix + "call_result:tenant-a:job-1"
    assert await real_redis.ttl(key) == -2
    await real_redis.set(key, "{}")
    assert await real_redis.ttl(key) == -1
    await real_redis.expire(key, _TTL)
    assert 0 < await real_redis.ttl(key) <= _TTL


async def test_one_mget_answers_each_language_in_place(
    real_redis: redis_async.Redis, key_prefix: str
) -> None:
    """read_whatsapps: a missing language is None at its own position."""
    keys = [key_prefix + whatsapp_key("tenant-a", "job-1", c) for c in ("en", "ru")]
    await real_redis.set(keys[1], '{"text": "x"}', ex=_TTL)
    assert await real_redis.mget(keys) == [None, '{"text": "x"}']
