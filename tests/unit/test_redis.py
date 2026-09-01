"""Redis client: the cost connection reads its configured URL, and readiness
reflects its ping result. Redis itself is mocked so tests need no running
server."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dodeal_ai.core import redis as redis_module


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
    _clear_caches()
    monkeypatch.setenv("DODEAL_REDIS_COST_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("DODEAL_REDIS_OPERATIONAL_URL", "redis://localhost:6379/2")
    from dodeal_ai.core.config import get_settings

    get_settings.cache_clear()

    urls = []
    with patch.object(
        redis_module.aioredis,
        "from_url",
        side_effect=lambda url, **kwargs: urls.append(url) or MagicMock(),
    ):
        redis_module.get_cost_client()
        redis_module.get_operational_client()

    assert urls == ["redis://localhost:6379/1", "redis://localhost:6379/2"]
    _clear_caches()


def test_the_two_clients_are_separate_logical_dbs():
    # Different failure policies (cost fails open, idempotency fails closed),
    # so an outage or a flush aimed at one must not reach the other.
    from dodeal_ai.core.config import Settings

    settings = Settings(_env_file=None, jwt_signing_key="test-key")
    assert settings.redis_cost_url != settings.redis_operational_url
    assert settings.redis_operational_url.endswith("/2")


async def test_ready_true_when_cost_redis_pings(monkeypatch):
    _clear_caches()
    fake = MagicMock()
    fake.ping = AsyncMock(return_value=True)
    with patch.object(redis_module.aioredis, "from_url", return_value=fake):
        assert await redis_module.check_cost_redis_ready() is True
    _clear_caches()


async def test_ready_false_when_ping_raises(monkeypatch):
    _clear_caches()
    fake = MagicMock()
    fake.ping = AsyncMock(side_effect=redis_module.redis.RedisError("down"))
    with patch.object(redis_module.aioredis, "from_url", return_value=fake):
        assert await redis_module.check_cost_redis_ready() is False
    _clear_caches()


async def test_ready_false_when_ping_returns_false(monkeypatch):
    _clear_caches()
    fake = MagicMock()
    fake.ping = AsyncMock(return_value=False)
    with patch.object(redis_module.aioredis, "from_url", return_value=fake):
        assert await redis_module.check_cost_redis_ready() is False
    _clear_caches()
