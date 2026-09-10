"""The cost gate's Lua script, actually executed.

Audit finding M5: the script in `core/cost/limiter.py` was never run by the
suite. Every existing cost test mocks the client, so the atomicity the gate is
built on -- and the TTL rule that decides whether a counter ever resets -- was
asserted only in prose. `fakeredis[lua]` executes real Lua against a real
server implementation in-process, so these run in the default lane with no
Redis to start and no marker to remember. A test that needs a genuine server
carries `redis_real` and is excluded from the default run; nothing does yet.

The script is IMPORTED, never retyped. A copied script would keep passing the
day the real one changed, which is the one failure a test like this exists to
prevent.
"""

from __future__ import annotations

import pytest
from fakeredis import FakeServer
from fakeredis import aioredis as fake_aioredis

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.cost import limiter
from dodeal_ai.core.cost.limiter import (
    _ADD_TOKENS_SCRIPT,
    _INCR_BOTH_SCRIPT,
    CostLimitError,
    _add_tokens_with_window,
    _incr_both_with_window,
    enforce_cost,
    get_usage,
)

_TENANT_KEY = "cost:tenant:tenant-a"
_USER_KEY = "cost:user:tenant-a:user-1"
_TOKEN_TENANT_KEY = "tokens:tenant:tenant-a"
_TOKEN_USER_KEY = "tokens:user:tenant-a:user-1"
_WINDOW = 60


@pytest.fixture
async def client():
    """A fresh in-process Redis with Lua enabled, isolated per test."""
    fake = fake_aioredis.FakeRedis(server=FakeServer(), decode_responses=True)
    yield fake
    await fake.aclose()


async def test_one_call_increments_both_counters(client):
    """Both keys move in a single EVAL, which is what makes the pair atomic."""
    tenant_count, user_count = await _incr_both_with_window(
        client, _TENANT_KEY, _USER_KEY, 3, _WINDOW
    )

    assert (tenant_count, user_count) == (3, 3)
    assert await client.get(_TENANT_KEY) == "3"
    assert await client.get(_USER_KEY) == "3"


async def test_the_returned_counts_are_the_stored_values(client):
    """The gate denies on the returned numbers, so they must be the stored ones."""
    await _incr_both_with_window(client, _TENANT_KEY, _USER_KEY, 7, _WINDOW)
    tenant_count, user_count = await _incr_both_with_window(
        client, _TENANT_KEY, _USER_KEY, 5, _WINDOW
    )

    assert (tenant_count, user_count) == (12, 12)
    assert int(await client.get(_TENANT_KEY)) == tenant_count
    assert int(await client.get(_USER_KEY)) == user_count


async def test_a_partial_key_set_still_lands_on_both(client):
    """A pre-existing tenant key must not leave the user key uncounted."""
    await client.set(_TENANT_KEY, 4)

    tenant_count, user_count = await _incr_both_with_window(
        client, _TENANT_KEY, _USER_KEY, 1, _WINDOW
    )

    assert (tenant_count, user_count) == (5, 1)


async def test_the_window_is_set_when_a_counter_is_created(client):
    """A created counter gets the window, or it would never reset."""
    await _incr_both_with_window(client, _TENANT_KEY, _USER_KEY, 1, _WINDOW)

    assert await client.ttl(_TENANT_KEY) == _WINDOW
    assert await client.ttl(_USER_KEY) == _WINDOW


async def test_a_later_increment_does_not_refresh_the_window(client):
    """The window is fixed at creation: a busy tenant must not push its own
    reset out of reach one request at a time."""
    await _incr_both_with_window(client, _TENANT_KEY, _USER_KEY, 1, _WINDOW)
    # A DIFFERENT, much larger window on the second call: if the script were
    # refreshing rather than creating, the TTL would jump to it.
    await _incr_both_with_window(client, _TENANT_KEY, _USER_KEY, 1, _WINDOW * 100)

    assert await client.ttl(_TENANT_KEY) <= _WINDOW
    assert await client.ttl(_USER_KEY) <= _WINDOW


async def test_a_pre_existing_key_without_a_ttl_never_gains_one(client):
    """Audit M4, pinned as it behaves TODAY: a counter that exists without a
    window keeps counting forever, and the script will not repair it."""
    await client.set(_TENANT_KEY, 1)
    assert await client.ttl(_TENANT_KEY) == -1

    await _incr_both_with_window(client, _TENANT_KEY, _USER_KEY, 1, _WINDOW)

    assert await client.ttl(_TENANT_KEY) == -1
    assert await client.ttl(_USER_KEY) == _WINDOW


def test_the_script_itself_makes_no_limit_decision():
    """The script counts; `enforce_cost` decides. Pinned so a future edit that
    moves the cap into Lua has to change this test deliberately."""
    assert "INCRBY" in _INCR_BOTH_SCRIPT
    assert "EXPIRE" in _INCR_BOTH_SCRIPT
    for absent in ("cost_per_tenant_limit", "quota", "deny"):
        assert absent not in _INCR_BOTH_SCRIPT


async def test_the_call_at_the_limit_denies_after_the_counter_moved(
    client, monkeypatch
):
    """The deny decision, end to end on real Lua: at the cap `enforce_cost`
    raises -- and the counter has ALREADY moved, because the script increments
    before Python compares."""
    monkeypatch.setenv("DODEAL_COST_PER_TENANT_LIMIT", "3")
    monkeypatch.setenv("DODEAL_COST_PER_USER_LIMIT", "3")
    monkeypatch.setattr(limiter, "get_cost_client", lambda: client)
    get_settings.cache_clear()

    for _ in range(3):
        await enforce_cost("tenant-a", "user-1")
    assert await get_usage("tenant-a", "user-1") == (3, 3)

    with pytest.raises(CostLimitError) as denied:
        await enforce_cost("tenant-a", "user-1")

    assert denied.value.reason_code == "tenant_quota_exceeded"
    # Increment-then-check: the fourth request is refused and counted.
    assert await get_usage("tenant-a", "user-1") == (4, 4)


# --- the token script, the same four properties ------------------------------
#
# A SECOND script, so a second set of executing tests. The two are separate
# constants on purpose (an edit aimed at one must not reach the other), and two
# copies of a test is what "separate" costs.


async def test_the_token_script_adds_to_both_counters(client):
    """Both token keys move in a single EVAL, which is what makes the pair
    atomic."""
    tenant_total, user_total = await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 120, _WINDOW
    )

    assert (tenant_total, user_total) == (120, 120)
    assert await client.get(_TOKEN_TENANT_KEY) == "120"
    assert await client.get(_TOKEN_USER_KEY) == "120"


async def test_the_returned_token_totals_are_the_stored_values(client):
    """The warning ratio is decided on the returned numbers, so they must be
    the stored ones."""
    await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 120, _WINDOW
    )
    tenant_total, user_total = await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 240, _WINDOW
    )

    assert (tenant_total, user_total) == (360, 360)
    assert int(await client.get(_TOKEN_TENANT_KEY)) == tenant_total
    assert int(await client.get(_TOKEN_USER_KEY)) == user_total


async def test_the_token_window_is_set_when_a_counter_is_created(client):
    """A created token counter gets the window, or a budget would never reset."""
    await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 120, _WINDOW
    )

    assert await client.ttl(_TOKEN_TENANT_KEY) == _WINDOW
    assert await client.ttl(_TOKEN_USER_KEY) == _WINDOW


async def test_a_later_token_charge_does_not_refresh_the_window(client):
    """The window is fixed at creation: a busy tenant must not push its own
    reset out of reach one charge at a time."""
    await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 120, _WINDOW
    )
    # A much larger window on the second call: a refreshing script would jump.
    await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 120, _WINDOW * 100
    )

    assert await client.ttl(_TOKEN_TENANT_KEY) <= _WINDOW
    assert await client.ttl(_TOKEN_USER_KEY) <= _WINDOW


def test_the_token_script_makes_no_limit_decision():
    """The script counts; `enforce_token_cost` warns and `token_preflight`
    denies. Pinned so moving either into Lua has to change this test."""
    assert "INCRBY" in _ADD_TOKENS_SCRIPT
    assert "EXPIRE" in _ADD_TOKENS_SCRIPT
    for absent in ("cost_tokens_per_tenant_limit", "warning", "deny"):
        assert absent not in _ADD_TOKENS_SCRIPT


async def test_the_two_scripts_touch_disjoint_keys(client):
    """The whole point of two scripts: one EVAL of each leaves four keys, and
    neither pair moved the other."""
    await _incr_both_with_window(client, _TENANT_KEY, _USER_KEY, 1, _WINDOW)
    await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 120, _WINDOW
    )

    assert sorted(await client.keys("*")) == sorted(
        [_TENANT_KEY, _USER_KEY, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY]
    )
    assert await client.get(_TENANT_KEY) == "1"
    assert await client.get(_TOKEN_TENANT_KEY) == "120"
