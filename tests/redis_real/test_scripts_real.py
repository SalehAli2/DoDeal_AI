"""The three Lua scripts and the reservation's SET NX / XX, on a real Redis server.

Each test names its hermetic counterpart in its docstring, so the two lanes can be
read side by side: tests/unit/test_cost_lua.py (fakeredis[lua]) for the scripts,
tests/unit/test_unit_a_state.py (FakeOperationalRedis) for the reservation. A test
with no counterpart says so: those are the ones a fake cannot run at all.

Every script, key spelling and stored value is IMPORTED from the module that uses
it, never retyped -- a copy keeps passing the day the original changes. Keys are
the production spellings under the per-test `key_prefix`; values are inert strings
and numbers. No note text, and nothing derived from one.

A TTL is READ, never waited for. Redis rounds TTL to the nearest second, so a window
read back in the same breath is the window itself; `_assert_window` allows one
second for a slow round trip and no more.
"""

from __future__ import annotations

import asyncio
import math

import pytest
from redis import asyncio as redis_async

from dodeal_ai.core.config import Settings
from dodeal_ai.core.cost.limiter import (
    _add_tokens_with_window,
    _incr_both_with_window,
    _token_keys,
)
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import (
    IDEMPOTENCY_INFLIGHT_MULTIPLIER,
)
from dodeal_ai.units.structured_intelligence.state import (
    _CONFIRMED,
    _RESERVED,
    _TAKE_RATE_LIMIT_SCRIPT,
    _TTL_NO_EXPIRY,
    _idempotency_key,
    _rate_limit_key,
)

pytestmark = [pytest.mark.redis_real, pytest.mark.asyncio(loop_scope="session")]

_TENANT = "tenant-a"
_SUBJECT = "user-1"
_WINDOW = 60
# What Redis answers TTL with for a key that does not exist.
_TTL_KEY_ABSENT = -2


def _assert_window(ttl: int, window: int = _WINDOW) -> None:
    assert window - 1 <= ttl <= window, f"TTL {ttl} is not a fresh {window}s window"


@pytest.fixture
def cost_keys(key_prefix: str) -> tuple[str, str]:
    """Gate 4's request counters, spelled as enforce_cost spells them."""
    return (
        f"{key_prefix}cost:tenant:{_TENANT}",
        f"{key_prefix}cost:user:{_TENANT}:{_SUBJECT}",
    )


@pytest.fixture
def token_keys(key_prefix: str) -> tuple[str, str]:
    tenant_key, user_key = _token_keys(_TENANT, _SUBJECT)
    return key_prefix + tenant_key, key_prefix + user_key


# --- (a) the request script -------------------------------------------------


async def test_the_cost_script_moves_both_counters_together(
    real_redis: redis_async.Redis, cost_keys: tuple[str, str]
) -> None:
    """Counterparts: test_one_call_increments_both_counters,
    test_the_returned_counts_are_the_stored_values."""
    assert await _incr_both_with_window(real_redis, *cost_keys, 3, _WINDOW) == (3, 3)
    assert await _incr_both_with_window(real_redis, *cost_keys, 4, _WINDOW) == (7, 7)

    assert await real_redis.mget(*cost_keys) == ["7", "7"]


async def test_the_cost_window_is_set_on_create_and_only_on_create(
    real_redis: redis_async.Redis, cost_keys: tuple[str, str]
) -> None:
    """Counterparts: test_the_window_is_set_when_a_counter_is_created,
    test_a_later_increment_does_not_refresh_the_window."""
    await _incr_both_with_window(real_redis, *cost_keys, 1, _WINDOW)
    for key in cost_keys:
        _assert_window(await real_redis.ttl(key))

    # A far larger window on the second call: a refreshing script would jump.
    await _incr_both_with_window(real_redis, *cost_keys, 1, _WINDOW * 100)
    for key in cost_keys:
        assert await real_redis.ttl(key) <= _WINDOW


async def test_a_cost_key_without_a_ttl_never_gains_one(
    real_redis: redis_async.Redis, cost_keys: tuple[str, str]
) -> None:
    """Counterpart: test_a_pre_existing_key_without_a_ttl_never_gains_one.

    Audit M4, pinned as it behaves TODAY, as its counterpart pins it: the request
    script sets EXPIRE only on create, so a counter that exists without a window
    keeps counting forever. The M4 fix changes this test and its counterpart in
    the same commit.
    """
    tenant_key, user_key = cost_keys
    await real_redis.set(tenant_key, 1)
    assert await real_redis.ttl(tenant_key) == _TTL_NO_EXPIRY

    await _incr_both_with_window(real_redis, tenant_key, user_key, 1, _WINDOW)

    assert await real_redis.ttl(tenant_key) == _TTL_NO_EXPIRY
    _assert_window(await real_redis.ttl(user_key))


# --- (b) the token script, and the line between the two ---------------------


async def test_the_token_script_moves_both_counters_together(
    real_redis: redis_async.Redis, token_keys: tuple[str, str]
) -> None:
    """Counterparts: test_the_token_script_adds_to_both_counters,
    test_the_returned_token_totals_are_the_stored_values."""
    first = await _add_tokens_with_window(real_redis, *token_keys, 120, _WINDOW)
    second = await _add_tokens_with_window(real_redis, *token_keys, 240, _WINDOW)

    assert (first, second) == ((120, 120), (360, 360))
    assert await real_redis.mget(*token_keys) == ["360", "360"]


async def test_the_token_window_is_set_on_create_and_only_on_create(
    real_redis: redis_async.Redis, token_keys: tuple[str, str]
) -> None:
    """Counterparts: test_the_token_window_is_set_when_a_counter_is_created,
    test_a_later_token_charge_does_not_refresh_the_window."""
    await _add_tokens_with_window(real_redis, *token_keys, 120, _WINDOW)
    for key in token_keys:
        _assert_window(await real_redis.ttl(key))

    await _add_tokens_with_window(real_redis, *token_keys, 120, _WINDOW * 100)
    for key in token_keys:
        assert await real_redis.ttl(key) <= _WINDOW


async def test_a_token_key_without_a_ttl_never_gains_one(
    real_redis: redis_async.Redis, token_keys: tuple[str, str]
) -> None:
    """No hermetic counterpart. test_cost_lua.py pins M4 on the request script
    only; the token script has the same EXISTS-then-EXPIRE shape and, as this
    shows, the same edge. Pinned as it behaves today, like the request script."""
    tenant_key, user_key = token_keys
    await real_redis.set(tenant_key, 1)

    await _add_tokens_with_window(real_redis, tenant_key, user_key, 120, _WINDOW)

    assert await real_redis.ttl(tenant_key) == _TTL_NO_EXPIRY
    _assert_window(await real_redis.ttl(user_key))


async def test_charging_tokens_leaves_the_request_counters_alone(
    real_redis: redis_async.Redis,
    cost_keys: tuple[str, str],
    token_keys: tuple[str, str],
) -> None:
    """Counterparts: test_the_two_scripts_touch_disjoint_keys, and at the route
    test_token_and_request_counters_never_touch."""
    await _incr_both_with_window(real_redis, *cost_keys, 1, _WINDOW)

    await _add_tokens_with_window(real_redis, *token_keys, 120, _WINDOW)

    assert await real_redis.mget(*cost_keys) == ["1", "1"]


async def test_counting_a_request_leaves_the_token_counters_alone(
    real_redis: redis_async.Redis,
    key_prefix: str,
    cost_keys: tuple[str, str],
    token_keys: tuple[str, str],
) -> None:
    """The reverse direction, and the whole key set: one EVAL of each script
    leaves exactly four keys. Counterpart: test_the_two_scripts_touch_disjoint_keys."""
    await _add_tokens_with_window(real_redis, *token_keys, 120, _WINDOW)

    await _incr_both_with_window(real_redis, *cost_keys, 1, _WINDOW)

    assert await real_redis.mget(*token_keys) == ["120", "120"]
    stored = [key async for key in real_redis.scan_iter(match=f"{key_prefix}*")]
    assert sorted(stored) == sorted([*cost_keys, *token_keys])


# --- (c) the rate limit's script: the check IS the increment ----------------

_RATE_LIMIT = 3


@pytest.fixture
def rate_key(key_prefix: str) -> str:
    return key_prefix + _rate_limit_key(_TENANT, _SUBJECT)


async def _take(
    client: redis_async.Redis,
    key: str,
    *,
    limit: int = _RATE_LIMIT,
    window: int = _WINDOW,
) -> list[int]:
    """One raw execution of the imported script: [allowed, count before]."""
    return await client.eval(_TAKE_RATE_LIMIT_SCRIPT, 1, key, limit, window)


async def test_the_rate_script_allows_up_to_the_limit_and_returns_the_count_before(
    real_redis: redis_async.Redis, rate_key: str
) -> None:
    """Counterparts: test_the_rate_script_increments_only_under_the_limit,
    test_the_rate_script_returns_the_count_before_the_call."""
    replies = [await _take(real_redis, rate_key) for _ in range(_RATE_LIMIT + 1)]

    assert replies == [[1, 0], [1, 1], [1, 2], [0, 3]]
    assert await real_redis.get(rate_key) == str(_RATE_LIMIT)


async def test_the_rate_window_is_set_when_the_counter_is_created(
    real_redis: redis_async.Redis, rate_key: str
) -> None:
    """Counterparts: the same name, and test_a_later_slot_does_not_refresh_the_rate_window."""
    await _take(real_redis, rate_key)
    _assert_window(await real_redis.ttl(rate_key))

    await _take(real_redis, rate_key, window=_WINDOW * 100)
    assert await real_redis.ttl(rate_key) <= _WINDOW


async def test_a_rate_key_without_a_ttl_gains_one_on_the_next_slot(
    real_redis: redis_async.Redis, rate_key: str
) -> None:
    """Counterpart: the same name. Audit M4, repaired for this key."""
    await real_redis.set(rate_key, 1)
    assert await real_redis.ttl(rate_key) == _TTL_NO_EXPIRY

    await _take(real_redis, rate_key)

    _assert_window(await real_redis.ttl(rate_key))


async def test_concurrent_slots_are_taken_exactly_up_to_the_limit(
    real_redis: redis_async.Redis, rate_key: str
) -> None:
    """No hermetic counterpart: fakeredis runs in-process, so nothing there is
    ever concurrent. Here the gathered EVALs each take their own connection and
    reach one server at once -- register item 27's claim that two judgements
    cannot both take the last slot.

    Exact, not "at most": every allowed reply saw a DIFFERENT count before it, and
    every refused one saw the full count.
    """
    limit = 10
    replies = await asyncio.gather(
        *(_take(real_redis, rate_key, limit=limit) for _ in range(limit + 5))
    )

    allowed = sorted(count for ok, count in replies if ok == 1)
    refused = [count for ok, count in replies if ok == 0]
    assert allowed == list(range(limit))
    assert refused == [limit] * 5
    assert await real_redis.get(rate_key) == str(limit)


# --- (d) the reservation: SET NX EX, SET XX EX, DEL -------------------------

# The in-flight lifetime at the default deadline, derived as the pipeline derives
# it, and the long one the confirm applies.
_SHORT_TTL = math.ceil(
    Settings.model_construct().judgement_deadline_seconds
    * IDEMPOTENCY_INFLIGHT_MULTIPLIER
)
_LONG_TTL = get_tenant_config(_TENANT).idempotency_ttl_seconds


@pytest.fixture
def reservation_key(key_prefix: str) -> str:
    # 64 zeros: a digest's length, and nobody's digest.
    return key_prefix + _idempotency_key(_TENANT, 42, "0" * 64)


async def test_a_reservation_is_claimed_once_and_refused_the_second_time(
    real_redis: redis_async.Redis, reservation_key: str
) -> None:
    """Counterparts: test_first_reservation_is_claimed,
    test_second_identical_reservation_is_refused, test_reservation_sets_the_ttl_on_create.

    `is True` and `is None`, not truthiness: FakeOperationalRedis claims a declined
    NX answers None rather than False, and this is where that claim is checked.
    """
    claim = await real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)
    again = await real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)

    assert claim is True
    assert again is None
    assert await real_redis.get(reservation_key) == _RESERVED
    _assert_window(await real_redis.ttl(reservation_key), _SHORT_TTL)


async def test_concurrent_identical_reservations_have_exactly_one_winner(
    real_redis: redis_async.Redis, reservation_key: str
) -> None:
    """No hermetic counterpart. SET NX is the reservation's whole atomicity (the
    state.py docstring): ten identical claims reaching the server at once, one wins."""
    claims = await asyncio.gather(
        *(
            real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)
            for _ in range(10)
        )
    )

    assert claims.count(True) == 1
    assert claims.count(None) == 9


async def test_a_confirm_on_a_missing_key_creates_nothing(
    real_redis: redis_async.Redis, reservation_key: str
) -> None:
    """Counterpart: test_confirm_uses_xx_and_never_creates_a_key. A reservation
    that already expired must stay gone."""
    confirmed = await real_redis.set(reservation_key, _CONFIRMED, xx=True, ex=_LONG_TTL)

    assert confirmed is None
    assert await real_redis.exists(reservation_key) == 0
    assert await real_redis.ttl(reservation_key) == _TTL_KEY_ABSENT


async def test_a_confirm_replaces_the_value_and_the_ttl(
    real_redis: redis_async.Redis, reservation_key: str
) -> None:
    """Counterparts: test_confirm_replaces_the_reservation_with_the_long_ttl,
    test_a_confirmed_key_is_still_a_duplicate."""
    await real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)

    confirmed = await real_redis.set(reservation_key, _CONFIRMED, xx=True, ex=_LONG_TTL)

    assert confirmed is True
    assert await real_redis.get(reservation_key) == _CONFIRMED
    _assert_window(await real_redis.ttl(reservation_key), _LONG_TTL)
    duplicate = await real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)
    assert duplicate is None


async def test_a_release_frees_the_note_for_a_new_reservation(
    real_redis: redis_async.Redis, reservation_key: str
) -> None:
    """Counterpart: test_release_removes_the_reservation."""
    await real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)

    assert await real_redis.delete(reservation_key) == 1

    assert await real_redis.exists(reservation_key) == 0
    reclaim = await real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)
    assert reclaim is True
