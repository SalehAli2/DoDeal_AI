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

The last section runs the prompt-slots db2 script (register items 27 and 119)
here, because this is the file where Lua actually runs.
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
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.state import (
    _SLOTS_ALLOWED,
    _SLOTS_DENIED_BY_ATTEMPT,
    _SLOTS_DENIED_BY_RATE,
    _TAKE_PROMPT_SLOTS_SCRIPT,
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


async def test_a_pre_existing_key_without_a_ttl_gains_the_window(client):
    """Audit M4, repaired: a counter found with no TTL gets the window on the next call."""
    await client.set(_TENANT_KEY, 1)
    await client.set(_USER_KEY, 1)
    assert await client.ttl(_TENANT_KEY) == -1

    await _incr_both_with_window(client, _TENANT_KEY, _USER_KEY, 1, _WINDOW)

    assert await client.ttl(_TENANT_KEY) == _WINDOW
    assert await client.ttl(_USER_KEY) == _WINDOW


async def test_a_pre_existing_key_with_a_ttl_keeps_it(client):
    """A counter that already has a window is not reset by the M4 repair."""
    await client.set(_TENANT_KEY, 1, ex=_WINDOW)
    await client.set(_USER_KEY, 1, ex=_WINDOW)

    await _incr_both_with_window(client, _TENANT_KEY, _USER_KEY, 1, _WINDOW * 100)

    assert 0 < await client.ttl(_TENANT_KEY) <= _WINDOW
    assert 0 < await client.ttl(_USER_KEY) <= _WINDOW


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


async def test_a_token_counter_without_a_ttl_gains_the_window(client):
    """Audit M4, repaired: a token counter found with no TTL gets the window on the next charge."""
    await client.set(_TOKEN_TENANT_KEY, 1)
    await client.set(_TOKEN_USER_KEY, 1)

    await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 120, _WINDOW
    )

    assert await client.ttl(_TOKEN_TENANT_KEY) == _WINDOW
    assert await client.ttl(_TOKEN_USER_KEY) == _WINDOW


async def test_a_token_counter_with_a_ttl_keeps_it(client):
    """A token counter that already has a window is not reset by the M4 repair."""
    await client.set(_TOKEN_TENANT_KEY, 1, ex=_WINDOW)
    await client.set(_TOKEN_USER_KEY, 1, ex=_WINDOW)

    await _add_tokens_with_window(
        client, _TOKEN_TENANT_KEY, _TOKEN_USER_KEY, 120, _WINDOW * 100
    )

    assert 0 < await client.ttl(_TOKEN_TENANT_KEY) <= _WINDOW
    assert 0 < await client.ttl(_TOKEN_USER_KEY) <= _WINDOW


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


# --- the prompt-slots script (db2): both guards checked and taken at once ------

_ATTEMPT_KEY = "attempt:tenant-a:1656:10"
_RATE_KEY = "ratelimit:tenant-a:42"
_ATTEMPT_CAP = 1
_ATTEMPT_TTL = 600
_RATE_LIMIT = 3


async def _take(
    client,
    attempt_key: str = _ATTEMPT_KEY,
    *,
    cap: int = _ATTEMPT_CAP,
    window: int = _WINDOW,
):
    """One raw execution of the imported script: [outcome, attempts, rate count]."""
    return await client.eval(
        _TAKE_PROMPT_SLOTS_SCRIPT,
        2,
        attempt_key,
        _RATE_KEY,
        cap,
        _ATTEMPT_TTL,
        _RATE_LIMIT,
        window,
    )


def _note(note_id: int) -> str:
    return f"attempt:tenant-a:1656:{note_id}"


async def test_an_allowed_take_increments_both_counters(client):
    """A take moves both keys by one and returns the new attempt count and the rate
    count before it."""
    assert await _take(client) == [_SLOTS_ALLOWED, 1, 0]
    assert await client.mget(_ATTEMPT_KEY, _RATE_KEY) == ["1", "1"]


async def test_the_attempt_cap_denies_and_leaves_the_rate_key_untouched(client):
    """At the attempt cap nothing is written: the rate key is neither moved nor
    created, and the attempt key keeps its value and its missing TTL."""
    await client.set(_ATTEMPT_KEY, _ATTEMPT_CAP)

    assert await _take(client) == [_SLOTS_DENIED_BY_ATTEMPT, _ATTEMPT_CAP, 0]
    assert await client.exists(_RATE_KEY) == 0
    assert await client.get(_ATTEMPT_KEY) == str(_ATTEMPT_CAP)
    assert await client.ttl(_ATTEMPT_KEY) == -1


async def test_the_rate_limit_denies_and_leaves_the_attempt_key_untouched(client):
    """At the rate limit nothing is written: the attempt key is neither moved nor
    created, and the rate key keeps its value and its missing TTL."""
    await client.set(_RATE_KEY, _RATE_LIMIT)

    assert await _take(client) == [_SLOTS_DENIED_BY_RATE, 0, _RATE_LIMIT]
    assert await client.exists(_ATTEMPT_KEY) == 0
    assert await client.get(_RATE_KEY) == str(_RATE_LIMIT)
    assert await client.ttl(_RATE_KEY) == -1


async def test_the_attempt_cap_is_checked_before_the_rate_limit(client):
    """Both at their caps: the note's cap is the reason, as decide() orders them."""
    await client.set(_ATTEMPT_KEY, _ATTEMPT_CAP)
    await client.set(_RATE_KEY, _RATE_LIMIT)

    assert (await _take(client))[0] == _SLOTS_DENIED_BY_ATTEMPT


async def test_a_second_take_on_one_note_is_denied_at_the_cap(client):
    """The race, in order: the second take for one note sees the first one's attempt."""
    replies = [await _take(client), await _take(client)]

    assert replies == [[_SLOTS_ALLOWED, 1, 0], [_SLOTS_DENIED_BY_ATTEMPT, 1, 0]]
    assert await client.mget(_ATTEMPT_KEY, _RATE_KEY) == ["1", "1"]


async def test_the_rate_slots_run_out_across_notes(client):
    """Three notes take the three slots and the fourth is refused, each reply
    carrying the rate count before its call."""
    replies = [await _take(client, _note(n)) for n in (10, 11, 12, 13)]

    assert replies == [
        [_SLOTS_ALLOWED, 1, 0],
        [_SLOTS_ALLOWED, 1, 1],
        [_SLOTS_ALLOWED, 1, 2],
        [_SLOTS_DENIED_BY_RATE, 0, _RATE_LIMIT],
    ]
    assert await client.get(_RATE_KEY) == str(_RATE_LIMIT)
    assert await client.exists(_note(13)) == 0


async def test_both_windows_are_set_when_the_counters_are_created(client):
    """Each counter gets its own window, or a note or a subject is limited forever."""
    await _take(client)

    assert await client.ttl(_ATTEMPT_KEY) == _ATTEMPT_TTL
    assert await client.ttl(_RATE_KEY) == _WINDOW


async def test_a_later_take_does_not_refresh_either_window(client):
    """Fixed, not sliding: a busy subject must not push its own reset away."""
    await _take(client, cap=2)
    await _take(client, cap=2, window=_WINDOW * 100)

    assert await client.ttl(_ATTEMPT_KEY) <= _ATTEMPT_TTL
    assert await client.ttl(_RATE_KEY) <= _WINDOW


async def test_keys_without_a_ttl_gain_one_on_the_next_take(client):
    """Counters with no window gain one on the next take (audit M4, repaired for
    both keys)."""
    await client.set(_ATTEMPT_KEY, 1)
    await client.set(_RATE_KEY, 1)

    await _take(client, cap=2)

    assert await client.ttl(_ATTEMPT_KEY) == _ATTEMPT_TTL
    assert await client.ttl(_RATE_KEY) == _WINDOW


async def test_take_prompt_slots_end_to_end_on_real_lua(client, monkeypatch):
    """Lua's integer replies reach the pipeline through take_prompt_slots as the
    (attempts, rate_allowed, rate_count) decide() reads, both counts before."""
    monkeypatch.setattr(state, "get_operational_client", lambda: client)

    async def take(note_id: int):
        return await state.take_prompt_slots(
            "tenant-a",
            1656,
            note_id,
            "42",
            attempt_cap=_ATTEMPT_CAP,
            attempt_ttl=_ATTEMPT_TTL,
            rate_limit=_RATE_LIMIT,
            rate_ttl=_WINDOW,
            attempts_read=0,
            request_id="req-1",
        )

    taken = [await take(n) for n in (10, 10, 11, 12, 13)]

    assert taken == [
        (0, True, 0),
        (_ATTEMPT_CAP, True, 0),
        (0, True, 1),
        (0, True, 2),
        (0, False, _RATE_LIMIT),
    ]
