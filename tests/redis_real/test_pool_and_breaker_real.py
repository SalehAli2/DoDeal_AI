"""The bounded pool and the breaker, on real sockets.

tests/unit/test_redis.py pins BoundedPool on stub connection classes that never
open a socket, and tests/unit/test_breaker.py drives the breaker with a fake clock
and a synthetic RedisError. Here the held connection is a real one, the pool's
refusal is redis-py's own, and the dead host is a real port with nothing on it.
Each test names its hermetic counterpart.

The pools are built the way core/redis.py's `_build_pool` builds them, from
Settings' DEFAULTS rather than from the environment: a local .env must not change
the connect budget a timing assertion is measured against.
"""

from __future__ import annotations

import socket
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis
from redis import asyncio as redis_async

from dodeal_ai.core.breaker import BreakerOpen, BreakerState, CircuitBreaker
from dodeal_ai.core.config import Settings
from dodeal_ai.core.redis import BoundedPool, PoolExhausted

pytestmark = [pytest.mark.redis_real, pytest.mark.asyncio(loop_scope="session")]

_DEFAULTS = Settings.model_construct()


def _pool(url: str, **overrides: Any) -> BoundedPool:
    """`_build_pool`'s arguments at their defaults, with `overrides` on top."""
    kwargs: dict[str, Any] = {
        "decode_responses": True,
        "max_connections": _DEFAULTS.redis_pool_size,
        "timeout": _DEFAULTS.redis_pool_acquire_timeout_seconds,
        "socket_connect_timeout": _DEFAULTS.redis_connect_timeout_seconds,
        "socket_timeout": _DEFAULTS.redis_socket_timeout_seconds,
    }
    kwargs.update(overrides)
    return BoundedPool.from_url(url, **kwargs)


@pytest.fixture
def closed_port_url(real_redis_url: str) -> str:
    """The lane's URL with its port swapped for one nothing listens on.

    The port is found by binding a socket on the same host and closing it again,
    so it was free a moment ago. Credentials and database are kept as they are.
    """
    parts = urlsplit(real_redis_url)
    host = parts.hostname or "localhost"
    family, _, _, _, address = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)[0]
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind(address)
            port = probe.getsockname()[1]
    except OSError:
        pytest.skip(
            "DODEAL_REDIS_REAL_URL names a host this machine cannot bind, "
            "so no closed port on it can be found"
        )
    userinfo = parts.netloc.rpartition("@")[0]
    hostport = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    netloc = f"{userinfo}@{hostport}" if userinfo else hostport
    return urlunsplit(parts._replace(netloc=netloc))


async def test_a_real_pool_at_its_cap_refuses_as_pool_exhausted(
    real_redis_url: str,
) -> None:
    """Counterpart: test_an_exhausted_pool_raises_pool_exhausted.

    The chain is the point (register item 81): BoundedPool recognises the acquire
    refusal ONLY by redis-py's ConnectionError being caused by the wait's own
    TimeoutError. That chain is asserted here as redis-py really builds it, and
    the pool is shown to recover once the connection is handed back.
    """
    pool = BoundedPool.from_url(real_redis_url, max_connections=1, timeout=0.05)
    try:
        held = await pool.get_connection()
        assert held.is_connected
        try:
            with pytest.raises(PoolExhausted) as refused:
                await pool.get_connection()
        finally:
            await pool.release(held)
        again = await pool.get_connection()
        await pool.release(again)
    finally:
        await pool.aclose()

    refusal = refused.value.__cause__
    assert type(refusal) is redis.ConnectionError
    assert isinstance(refusal.__cause__, TimeoutError)


async def test_a_dead_host_fails_as_the_store_and_not_as_the_pool(
    closed_port_url: str,
) -> None:
    """Counterpart: test_a_socket_that_will_not_connect_is_not_pool_exhaustion.

    WHICH store error depends on the host, not on this code. A refused connect is
    ConnectionError where the OS refuses at once; it is TimeoutError where the
    connect outlives Settings' connect budget first -- a Windows loopback, or any
    firewall that drops rather than refuses. Both are the store failing, neither
    is relabelled, and the breaker counts both. The stub counterpart models only
    the first.
    """
    client = redis_async.Redis.from_pool(_pool(closed_port_url))
    try:
        with pytest.raises(redis.RedisError) as caught:
            await client.ping()
    finally:
        await client.aclose()

    assert isinstance(caught.value, redis.ConnectionError | redis.TimeoutError)
    assert not isinstance(caught.value, PoolExhausted)


async def test_the_breaker_opens_on_a_dead_host_and_then_refuses_without_a_socket(
    closed_port_url: str,
) -> None:
    """Counterparts: test_a_refused_connection_is_counted, test_the_threshold_opens_it,
    test_an_open_breaker_refuses_without_calling.

    "Without a socket" twice over: the factory is not called a third time, and
    the refusal returns inside the connect budget a real attempt would spend.
    """
    breaker = CircuitBreaker("real-lane", failure_threshold=2, open_seconds=60)
    client = redis_async.Redis.from_pool(_pool(closed_port_url))
    calls = 0

    async def ping() -> bool:
        nonlocal calls
        calls += 1
        return await client.ping()

    try:
        for state_after in (BreakerState.CLOSED, BreakerState.OPEN):
            with pytest.raises(redis.RedisError) as failed:
                await breaker.call(ping)
            assert not isinstance(failed.value, BreakerOpen | PoolExhausted)
            assert breaker.state is state_after

        started = time.monotonic()
        with pytest.raises(BreakerOpen):
            await breaker.call(ping)
        refused_in = time.monotonic() - started
    finally:
        await client.aclose()

    assert calls == 2
    assert refused_in < _DEFAULTS.redis_connect_timeout_seconds


async def test_a_live_host_keeps_the_breaker_closed_and_clears_its_count(
    real_redis_url: str, closed_port_url: str, key_prefix: str
) -> None:
    """Counterparts: test_a_closed_breaker_passes_the_call_through,
    test_a_success_resets_the_count.

    The count is private, so it is read the one way the breaker shows it. One
    dead-host failure first, so there IS a count to clear. Then three live
    commands. At a threshold of two, the next failure must leave the breaker
    CLOSED, which it can do only from a count of zero. The one after must open
    it, which shows the count is really being kept.
    """
    breaker = CircuitBreaker("real-lane", failure_threshold=2, open_seconds=60)
    live = redis_async.Redis.from_pool(_pool(real_redis_url))
    dead = redis_async.Redis.from_pool(_pool(closed_port_url))
    key = f"{key_prefix}breaker"
    try:
        with pytest.raises(redis.RedisError):
            await breaker.call(dead.ping)

        assert await breaker.call(live.ping) is True
        assert await breaker.call(lambda: live.set(key, "inert")) is True
        assert await breaker.call(lambda: live.get(key)) == "inert"
        assert breaker.state is BreakerState.CLOSED

        with pytest.raises(redis.RedisError):
            await breaker.call(dead.ping)
        assert breaker.state is BreakerState.CLOSED
        with pytest.raises(redis.RedisError):
            await breaker.call(dead.ping)
        assert breaker.state is BreakerState.OPEN
    finally:
        await live.aclose()
        await dead.aclose()
