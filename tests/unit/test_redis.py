"""Redis clients: each reads its own URL, both get a bounded pool sized from
Settings, and readiness reflects each connection's ping. No test opens a live
Redis -- building a pool opens no socket, and the ping tests use a fake."""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis
from redis import asyncio as aioredis

from dodeal_ai.core import redis as redis_module
from dodeal_ai.core.config import REDIS_POOL_HEADROOM, Settings

_REDIS_MODULE_PATH = pathlib.Path("src/dodeal_ai/core/redis.py")


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    from dodeal_ai.core.config import get_settings

    get_settings.cache_clear()
    _clear_caches()
    yield
    get_settings.cache_clear()
    _clear_caches()


def _clear_caches():
    redis_module.get_cost_client.cache_clear()
    redis_module.get_operational_client.cache_clear()


def test_each_client_reads_its_own_url(monkeypatch):
    # A copy-paste between these two factories would put idempotency
    # reservations in the cost DB, where they would be counted as spend and
    # flushed on a different schedule. Assert each reads its OWN setting.
    monkeypatch.setenv("DODEAL_REDIS_COST_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("DODEAL_REDIS_OPERATIONAL_URL", "redis://localhost:6379/2")
    from dodeal_ai.core.config import get_settings

    get_settings.cache_clear()

    cost_kwargs = redis_module.get_cost_client().connection_pool.connection_kwargs
    op_kwargs = redis_module.get_operational_client().connection_pool.connection_kwargs

    assert cost_kwargs["db"] == 1
    assert op_kwargs["db"] == 2


def test_the_two_clients_are_separate_logical_dbs():
    # Different failure policies (cost fails open, idempotency fails closed),
    # so an outage or a flush aimed at one must not reach the other.
    from dodeal_ai.core.config import Settings

    settings = Settings(_env_file=None, jwt_signing_key="test-key")
    assert settings.redis_cost_url != settings.redis_operational_url
    assert settings.redis_operational_url.endswith("/2")


def test_the_two_factories_hold_separate_pools():
    """One pool per connection: a shared pool would let db1 exhaust db2's
    connections, which is the coupling the two DBs exist to avoid."""
    cost_pool = redis_module.get_cost_client().connection_pool
    operational_pool = redis_module.get_operational_client().connection_pool

    assert cost_pool is not operational_pool


def test_each_factory_is_cached():
    """Cached, so one pool is reused. A per-call client would make the bound on
    connections meaningless -- every caller would get twenty of its own."""
    assert redis_module.get_cost_client() is redis_module.get_cost_client()
    assert (
        redis_module.get_operational_client() is redis_module.get_operational_client()
    )


@pytest.mark.parametrize("factory_name", ["get_cost_client", "get_operational_client"])
def test_the_pool_is_bounded_and_sized_from_settings(monkeypatch, factory_name):
    """Both pools, not just the cost one: a bound on one connection and none on
    the other is the same file-descriptor exhaustion arriving by the other
    door."""
    monkeypatch.setenv("DODEAL_REDIS_CONNECT_TIMEOUT_SECONDS", "0.4")
    monkeypatch.setenv("DODEAL_REDIS_SOCKET_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("DODEAL_REDIS_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("DODEAL_REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS", "3.5")
    from dodeal_ai.core.config import get_settings

    get_settings.cache_clear()

    pool = getattr(redis_module, factory_name)().connection_pool

    assert isinstance(pool, redis_module.BoundedPool)
    assert isinstance(pool, aioredis.BlockingConnectionPool)
    assert pool.max_connections == 7
    assert pool.timeout == 3.5
    assert pool.connection_kwargs["socket_connect_timeout"] == 0.4
    assert pool.connection_kwargs["socket_timeout"] == 2.5


def test_the_defaults_are_the_recorded_provisional_values():
    """The four defaults a deployment inherits if it configures nothing. The pool
    size is DERIVED: the in-flight cap's 32 plus the headroom's 4."""
    pool = redis_module.get_cost_client().connection_pool

    assert pool.max_connections == 36
    assert pool.timeout == 1.0
    assert pool.connection_kwargs["socket_connect_timeout"] == 0.25
    assert pool.connection_kwargs["socket_timeout"] == 1.0


# --- the pool is sized from the in-flight cap (register item 81) ------------


def test_the_pool_size_is_the_in_flight_cap_plus_headroom():
    """Unset, the pool holds one connection per request the app admits and then
    some: a pool of 20 under a cap of 32 was twelve admitted requests queueing on
    Redis connections instead of on nothing."""
    settings = Settings(_env_file=None, jwt_signing_key="k")

    assert settings.redis_max_connections is None
    assert settings.redis_pool_size == 36
    assert settings.redis_pool_size == settings.max_inflight + REDIS_POOL_HEADROOM


def test_the_pool_size_follows_the_in_flight_cap(monkeypatch):
    """Derived, not defaulted: raising the cap raises the pool without a second
    setting to remember."""
    monkeypatch.setenv("DODEAL_MAX_INFLIGHT", "10")
    from dodeal_ai.core.config import get_settings

    get_settings.cache_clear()

    assert redis_module.get_operational_client().connection_pool.max_connections == 14


def test_an_explicit_pool_size_is_honoured():
    """The override wins as given -- the deployment that sets it owns the number."""
    settings = Settings(_env_file=None, jwt_signing_key="k", redis_max_connections=50)

    assert settings.redis_pool_size == 50


# --- an exhausted pool says so, and only the acquire does -------------------


class _NoSocket(aioredis.Connection):
    """A connection that never opens a socket: the ACQUIRE is under test here,
    so connecting succeeds by doing nothing."""

    async def connect(self) -> None:
        return None

    async def can_read(self) -> bool:
        return False


class _Refused(aioredis.Connection):
    """A connection whose socket will not connect -- the store's own failure."""

    async def connect(self) -> None:
        raise redis.ConnectionError("refused")


def test_pool_exhausted_is_a_connection_error():
    """So every existing RedisError handler keeps its policy for the one call."""
    assert issubclass(redis_module.PoolExhausted, redis.ConnectionError)


async def test_an_exhausted_pool_raises_pool_exhausted():
    """Two concurrent acquires on a pool of one: the first holds the connection,
    and the second is refused by the pool after its wait -- as PoolExhausted."""
    pool = redis_module.BoundedPool(
        max_connections=1, timeout=0.01, connection_class=_NoSocket
    )

    results = await asyncio.gather(
        pool.get_connection(), pool.get_connection(), return_exceptions=True
    )

    held = [r for r in results if isinstance(r, aioredis.Connection)]
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(held) == 1
    assert len(refused) == 1
    assert isinstance(refused[0], redis_module.PoolExhausted)
    await pool.release(held[0])


async def test_a_socket_that_will_not_connect_is_not_pool_exhaustion():
    """Only the acquire is relabelled. A refused socket is evidence about the
    store, so it must reach the breaker as the plain ConnectionError it is."""
    pool = redis_module.BoundedPool(
        max_connections=1, timeout=0.01, connection_class=_Refused
    )

    with pytest.raises(redis.ConnectionError) as caught:
        await pool.get_connection()

    assert not isinstance(caught.value, redis_module.PoolExhausted)


def test_no_number_is_hardcoded_in_the_redis_module():
    """The whole point of the four settings: a literal here would be a second
    source of truth, and the deployment would tune the one that no longer wins.

    The module is PARSED rather than grepped -- `db1` and `db2` in the docstring
    would defeat a regex over the raw text, and a numeric literal cannot hide
    from the AST the way it can from a line-oriented pattern.
    """
    tree = ast.parse(_REDIS_MODULE_PATH.read_text(encoding="utf-8"))
    literals = [
        f"line {node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]

    assert not literals, (
        "numeric literal in core/redis.py -- every timeout and pool bound "
        "belongs in Settings:\n" + "\n".join(literals)
    )


async def test_cost_ready_true_when_it_pings():
    fake = MagicMock()
    fake.ping = AsyncMock(return_value=True)
    with patch.object(redis_module, "get_cost_client", return_value=fake):
        assert await redis_module.check_cost_redis_ready() is True


async def test_cost_ready_false_when_ping_raises():
    fake = MagicMock()
    fake.ping = AsyncMock(side_effect=redis_module.redis.RedisError("down"))
    with patch.object(redis_module, "get_cost_client", return_value=fake):
        assert await redis_module.check_cost_redis_ready() is False


async def test_cost_ready_false_when_ping_returns_false():
    fake = MagicMock()
    fake.ping = AsyncMock(return_value=False)
    with patch.object(redis_module, "get_cost_client", return_value=fake):
        assert await redis_module.check_cost_redis_ready() is False


async def test_operational_ready_true_when_it_pings():
    fake = MagicMock()
    fake.ping = AsyncMock(return_value=True)
    with patch.object(redis_module, "get_operational_client", return_value=fake):
        assert await redis_module.check_operational_redis_ready() is True


async def test_operational_ready_false_when_ping_raises():
    fake = MagicMock()
    fake.ping = AsyncMock(side_effect=redis_module.redis.RedisError("down"))
    with patch.object(redis_module, "get_operational_client", return_value=fake):
        assert await redis_module.check_operational_redis_ready() is False


async def test_the_two_probes_are_independent():
    """db1 up and db2 down is a real state, and each probe must report only its
    own connection -- a shared answer would hide exactly the case that matters."""
    up = MagicMock()
    up.ping = AsyncMock(return_value=True)
    down = MagicMock()
    down.ping = AsyncMock(side_effect=redis_module.redis.RedisError("down"))

    with (
        patch.object(redis_module, "get_cost_client", return_value=up),
        patch.object(redis_module, "get_operational_client", return_value=down),
    ):
        assert await redis_module.check_cost_redis_ready() is True
        assert await redis_module.check_operational_redis_ready() is False


def test_the_jobs_and_queue_clients_read_their_own_urls(monkeypatch):
    """db3 for jobs (register item 50); db0 for pushes to arq, undecoded because
    arq's job bodies are bytes; both bounded pools of their own."""
    from arq.connections import ArqRedis

    from dodeal_ai.core.config import get_settings

    monkeypatch.setenv("DODEAL_REDIS_JOBS_URL", "redis://localhost:6379/3")
    monkeypatch.setenv("DODEAL_REDIS_QUEUE_URL", "redis://localhost:6379/0")
    get_settings.cache_clear()
    redis_module.get_jobs_client.cache_clear()
    redis_module.get_queue_client.cache_clear()

    jobs_pool = redis_module.get_jobs_client().connection_pool
    queue = redis_module.get_queue_client()

    assert isinstance(queue, ArqRedis)
    assert isinstance(jobs_pool, redis_module.BoundedPool)
    assert isinstance(queue.connection_pool, redis_module.BoundedPool)
    assert jobs_pool.connection_kwargs["db"] == 3
    assert jobs_pool.connection_kwargs["decode_responses"] is True
    assert queue.connection_pool.connection_kwargs["db"] == 0
    assert queue.connection_pool.connection_kwargs["decode_responses"] is False
    assert redis_module.get_jobs_client() is redis_module.get_jobs_client()
    redis_module.get_jobs_client.cache_clear()
    redis_module.get_queue_client.cache_clear()
