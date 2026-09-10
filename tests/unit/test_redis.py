"""Redis clients: each reads its own URL, both get a bounded pool sized from
Settings, and readiness reflects each connection's ping. No test opens a live
Redis -- building a pool opens no socket, and the ping tests use a fake."""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis import asyncio as aioredis

from dodeal_ai.core import redis as redis_module

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

    assert isinstance(pool, aioredis.BlockingConnectionPool)
    assert pool.max_connections == 7
    assert pool.timeout == 3.5
    assert pool.connection_kwargs["socket_connect_timeout"] == 0.4
    assert pool.connection_kwargs["socket_timeout"] == 2.5


def test_the_defaults_are_the_recorded_provisional_values():
    """The four defaults a deployment inherits if it configures nothing."""
    pool = redis_module.get_cost_client().connection_pool

    assert pool.max_connections == 20
    assert pool.timeout == 1.0
    assert pool.connection_kwargs["socket_connect_timeout"] == 0.25
    assert pool.connection_kwargs["socket_timeout"] == 1.0


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
