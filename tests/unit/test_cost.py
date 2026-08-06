"""Cost gate: under limit passes, over limit denies, per-tenant and per-user
counted separately, atomic incr-by-amount, read-only usage, and Redis-down
fails OPEN with a warning (leaving neither counter touched). Redis is faked
in-memory so tests need no server."""

from __future__ import annotations

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.cost import limiter
from dodeal_ai.core.cost.limiter import CostLimitError, enforce_cost, get_usage


class FakeRedis:
    """Minimal in-memory stand-in for the two calls enforce_cost/get_usage
    make (EVAL of the atomic incr-both script, and MGET for read-only usage).
    Logic only: no persistence, no cross-worker sharing, no real Lua
    interpreter -- it just reproduces what the script does against a local
    dict."""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.raise_on_eval = False

    def eval(self, script, numkeys, *keys_and_args):
        if self.raise_on_eval:
            raise limiter.redis.RedisError("down")
        keys = keys_and_args[:numkeys]
        amount = int(keys_and_args[numkeys])
        # window (keys_and_args[numkeys + 1]) isn't modeled -- no TTL
        # semantics needed for these tests.
        counts = []
        for key in keys:
            self.store[key] = self.store.get(key, 0) + amount
            counts.append(self.store[key])
        return counts

    def mget(self, *keys):
        return [self.store.get(k) for k in keys]


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
    fake.raise_on_eval = True
    with caplog.at_level("WARNING", logger="dodeal_ai.cost"):
        enforce_cost("nasir3", "42")  # must NOT raise
    assert any("cost_cap_bypassed" in r.getMessage() for r in caplog.records)


def test_atomic_failure_leaves_neither_counter_touched(fake, caplog):
    # Tenant and user counters increment in one atomic script execution. If
    # Redis fails, neither counter should show any change -- not a partial
    # update where one moved and the other didn't.
    fake.raise_on_eval = True
    with caplog.at_level("WARNING", logger="dodeal_ai.cost"):
        enforce_cost("nasir3", "42")
    assert get_usage("nasir3", "42") == (0, 0)


def test_both_counters_move_together_on_success(fake):
    enforce_cost("nasir3", "42")
    assert get_usage("nasir3", "42") == (1, 1)


def test_amount_increments_both_counters_by_amount(fake):
    enforce_cost("nasir3", "42", amount=2)
    assert get_usage("nasir3", "42") == (2, 2)


def test_amount_can_exceed_cap_in_a_single_call(fake):
    with pytest.raises(CostLimitError) as exc:
        enforce_cost("nasir3", "42", amount=3)  # user cap is 2
    assert exc.value.reason_code == "user_quota_exceeded"


def test_get_usage_does_not_increment(fake):
    enforce_cost("nasir3", "42")
    before = get_usage("nasir3", "42")
    after = get_usage("nasir3", "42")
    assert before == after == (1, 1)


def test_get_usage_zero_when_untouched(fake):
    assert get_usage("nasir3", "42") == (0, 0)
