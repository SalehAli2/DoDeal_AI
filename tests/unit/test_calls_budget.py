"""The call budgets (register item 105): a "calls" scope reads and charges
`tokens:calls:tenant` only, audio seconds are charged on download, and over a
budget or with the store down a call job PAUSES rather than spending blind."""

from __future__ import annotations

import logging

import pytest

from dodeal_ai.core.breaker import cost_breaker
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext, TenantScope
from dodeal_ai.core.cost import limiter
from dodeal_ai.core.cost.limiter import (
    CallsBudgetPaused,
    calls_budget_preflight,
    charge_audio_seconds,
    enforce_calls_cost,
    enforce_token_cost,
    token_preflight,
)
from dodeal_ai.core.errors import TokenBudgetExceeded
from tests.conftest import RedisFakes
from tests.helpers.fake_cost_redis import FakeCostRedis

TOKENS = "tokens:calls:tenant:tenant-a"
AUDIO = "audio_seconds:calls:tenant:tenant-a"
PUSHES = "cost:calls:tenant:tenant-a"


def _calls_scope() -> TenantScope:
    return RequestContext(
        tenant="tenant-a",
        subject="service",
        database="",
        roles=(),
        permissions=frozenset(),
        request_id="req-call",
        principal="service",
    ).scope_for_author(27, budget="calls")


async def test_calls_charges_touch_only_calls_keys(redis_fakes: RedisFakes) -> None:
    """The guard: the push counter, the token charge, both pre-flights and the
    audio charge leave nothing but the three calls keys in db1."""
    scope = _calls_scope()
    await enforce_calls_cost("tenant-a")
    await token_preflight(scope)
    await enforce_token_cost(scope, input_tokens=900, output_tokens=100, profile="p")
    await calls_budget_preflight(scope)
    assert await charge_audio_seconds(scope, 95) == 95

    assert redis_fakes.cost.store == {PUSHES: 1, TOKENS: 1000, AUDIO: 95}
    read = {key for keys in redis_fakes.cost.mgets for key in keys}
    assert read == {TOKENS, AUDIO}


async def test_the_calls_token_budget_refuses_at_its_limit(
    redis_fakes: RedisFakes, monkeypatch
) -> None:
    monkeypatch.setenv("DODEAL_COST_TOKENS_CALLS_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    redis_fakes.cost.store[TOKENS] = 1000
    with pytest.raises(TokenBudgetExceeded):
        await token_preflight(_calls_scope())


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        ({TOKENS: 20_000_000}, "token_budget_exceeded"),
        ({AUDIO: 360_000}, "audio_budget_exceeded"),
    ],
)
async def test_at_either_calls_limit_the_job_pauses(
    redis_fakes: RedisFakes, stored: dict, reason: str
) -> None:
    redis_fakes.cost.store.update(stored)
    with pytest.raises(CallsBudgetPaused) as caught:
        await calls_budget_preflight(_calls_scope())
    assert caught.value.reason_code == reason


async def test_under_both_limits_the_preflight_passes(redis_fakes: RedisFakes) -> None:
    redis_fakes.cost.store.update({TOKENS: 19_999_999, AUDIO: 359_999})
    await calls_budget_preflight(_calls_scope())


async def test_an_outage_pauses_the_preflight_and_the_charge(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail CLOSED for a worker: no store, no spend."""
    down = FakeCostRedis(fail=True)
    monkeypatch.setattr(limiter, "get_cost_client", lambda: down)
    caplog.set_level(logging.WARNING, logger="dodeal_ai.cost")

    for attempt in (
        calls_budget_preflight(_calls_scope()),
        charge_audio_seconds(_calls_scope(), 95),
    ):
        with pytest.raises(CallsBudgetPaused) as caught:
            await attempt
        assert caught.value.reason_code == "cost_store_unavailable"
    lines = [r for r in caplog.records if r.getMessage() == "calls_budget_unavailable"]
    assert [line.__dict__["tenant"] for line in lines] == ["tenant-a", "tenant-a"]


async def test_an_open_breaker_pauses_and_says_so(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    down = FakeCostRedis(fail=True)
    monkeypatch.setattr(limiter, "get_cost_client", lambda: down)
    for _ in range(get_settings().breaker_failure_threshold):
        with pytest.raises(CallsBudgetPaused):
            await calls_budget_preflight(_calls_scope())
    assert cost_breaker().state.value == "open"
    caplog.set_level(logging.WARNING, logger="dodeal_ai.cost")
    caplog.clear()

    with pytest.raises(CallsBudgetPaused):
        await calls_budget_preflight(_calls_scope())
    (line,) = [
        r for r in caplog.records if r.getMessage() == "calls_budget_unavailable"
    ]
    assert line.__dict__["breaker"] == "open"


async def test_the_audio_charge_is_one_info_line_of_numbers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="dodeal_ai.cost")
    await charge_audio_seconds(_calls_scope(), 30)
    await charge_audio_seconds(_calls_scope(), 12)
    totals = [
        (r.__dict__["audio_seconds"], r.__dict__["calls_total"])
        for r in caplog.records
        if r.getMessage() == "audio_seconds_charged"
    ]
    assert totals == [(30, 30), (12, 42)]
