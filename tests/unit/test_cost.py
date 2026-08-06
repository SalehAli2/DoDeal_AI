"""Cost gate: under limit passes, over limit denies, per-tenant and per-user
counted separately, and Redis-down fails OPEN with a warning. Redis is faked
in-memory so tests need no server."""
from __future__ import annotations

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.cost import limiter
from dodeal_ai.core.cost.limiter import CostLimitError, enforce_cost


class FakeRedis:
    """Minimal in-memory stand-in supporting incr/expire, for logic tests only.
    Not a real cap: no persistence, no cross-worker sharing."""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.raise_on_incr = False

    def incr(self, key: str) -> int:
        if self.raise_on_incr:
            raise limiter.redis.RedisError("down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key: str, window: int) -> None:
        pass


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_COST_PER_TENANT_LIMIT", "5")
    monkeypatch.setenv("DODEAL_COST_PER_USER_LIMIT", "2")
    get_settings.cache_clear()
    r = FakeRedis()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: r)
    yield r
    get_settings.cache_clear()


def test_under_limit_passes(fake):
    enforce_cost("nasir3", "42")


def test_user_limit_denies(fake):
    enforce_cost("nasir3", "42")
    enforce_cost("nasir3", "42")
    with pytest.raises(CostLimitError) as exc:
        enforce_cost("nasir3", "42")
    assert exc.value.reason_code == "user_quota_exceeded"


def test_tenant_limit_denies(fake):
    for i in range(5):
        enforce_cost("nasir3", f"user{i}")
    with pytest.raises(CostLimitError) as exc:
        enforce_cost("nasir3", "user5")
    assert exc.value.reason_code == "tenant_quota_exceeded"


def test_tenants_counted_separately(fake):
    enforce_cost("tenantA", "u1")
    enforce_cost("tenantA", "u1")
    enforce_cost("tenantB", "u1")


def test_redis_down_fails_open_with_warning(fake, caplog):
    fake.raise_on_incr = True
    with caplog.at_level("WARNING", logger="dodeal_ai.cost"):
        enforce_cost("nasir3", "42")  # must NOT raise
    assert any("cost_cap_bypassed" in r.getMessage() for r in caplog.records)