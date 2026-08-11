"""Redis client: named connections read the configured URLs; readiness
reflects the cost connection's ping result (the queue connection has no
consumer yet). Redis itself is mocked so tests need no running server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    redis_module.get_queue_client.cache_clear()
    redis_module.get_cost_client.cache_clear()


def test_ready_true_when_cost_redis_pings(monkeypatch):
    _clear_caches()
    fake = MagicMock()
    fake.ping.return_value = True
    with patch.object(redis_module.redis, "from_url", return_value=fake):
        assert redis_module.check_cost_redis_ready() is True
    _clear_caches()


def test_ready_false_when_ping_raises(monkeypatch):
    _clear_caches()
    fake = MagicMock()
    fake.ping.side_effect = redis_module.redis.RedisError("down")
    with patch.object(redis_module.redis, "from_url", return_value=fake):
        assert redis_module.check_cost_redis_ready() is False
    _clear_caches()


def test_ready_false_when_ping_returns_false(monkeypatch):
    _clear_caches()
    fake = MagicMock()
    fake.ping.return_value = False
    with patch.object(redis_module.redis, "from_url", return_value=fake):
        assert redis_module.check_cost_redis_ready() is False
    _clear_caches()
