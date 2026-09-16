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
from dodeal_ai.units.structured_intelligence.decide import decide
from dodeal_ai.units.structured_intelligence.pipeline import (
    IDEMPOTENCY_INFLIGHT_MULTIPLIER,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    Decision,
    NoteAnalysis,
    NoteScore,
    NoteType,
    PromptWithheld,
)
from dodeal_ai.units.structured_intelligence.state import (
    _RESERVED,
    _SLOTS_ALLOWED,
    _SLOTS_DENIED_BY_ATTEMPT,
    _SLOTS_DENIED_BY_RATE,
    _TAKE_OVER_SCRIPT,
    _TAKE_PROMPT_SLOTS_SCRIPT,
    _TTL_NO_EXPIRY,
    _attempt_key,
    _idempotency_key,
    _prompt_slots_answer,
    _rate_limit_key,
)

pytestmark = [pytest.mark.redis_real, pytest.mark.asyncio(loop_scope="session")]

# What a confirmed key holds since items 1 and 2: judgement JSON. Its content is
# not what these SET XX tests are about, only that it replaces the reservation.
_CONFIRMED = '{"note_id": 42}'

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


async def test_a_cost_key_without_a_ttl_gains_the_window(
    real_redis: redis_async.Redis, cost_keys: tuple[str, str]
) -> None:
    """Counterpart: test_a_pre_existing_key_without_a_ttl_gains_the_window."""
    for key in cost_keys:
        await real_redis.set(key, 1)
        assert await real_redis.ttl(key) == _TTL_NO_EXPIRY

    await _incr_both_with_window(real_redis, *cost_keys, 1, _WINDOW)

    for key in cost_keys:
        assert 0 < await real_redis.ttl(key) <= _WINDOW


async def test_a_cost_key_with_a_ttl_keeps_it(
    real_redis: redis_async.Redis, cost_keys: tuple[str, str]
) -> None:
    """Counterpart: test_a_pre_existing_key_with_a_ttl_keeps_it."""
    for key in cost_keys:
        await real_redis.set(key, 1, ex=_WINDOW)

    await _incr_both_with_window(real_redis, *cost_keys, 1, _WINDOW * 100)

    for key in cost_keys:
        assert 0 < await real_redis.ttl(key) <= _WINDOW


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


async def test_a_token_key_without_a_ttl_gains_the_window(
    real_redis: redis_async.Redis, token_keys: tuple[str, str]
) -> None:
    """Counterpart: test_a_token_counter_without_a_ttl_gains_the_window."""
    for key in token_keys:
        await real_redis.set(key, 1)
        assert await real_redis.ttl(key) == _TTL_NO_EXPIRY

    await _add_tokens_with_window(real_redis, *token_keys, 120, _WINDOW)

    for key in token_keys:
        assert 0 < await real_redis.ttl(key) <= _WINDOW


async def test_a_token_key_with_a_ttl_keeps_it(
    real_redis: redis_async.Redis, token_keys: tuple[str, str]
) -> None:
    """Counterpart: test_a_token_counter_with_a_ttl_keeps_it."""
    for key in token_keys:
        await real_redis.set(key, 1, ex=_WINDOW)

    await _add_tokens_with_window(real_redis, *token_keys, 120, _WINDOW * 100)

    for key in token_keys:
        assert 0 < await real_redis.ttl(key) <= _WINDOW


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


# --- (c) the prompt-slots script: each check IS its increment ---------------

_NOTE_ID = 10
_ATTEMPT_CAP = 1
_ATTEMPT_TTL = 600
_RATE_LIMIT = 3


@pytest.fixture
def rate_key(key_prefix: str) -> str:
    return key_prefix + _rate_limit_key(_TENANT, _SUBJECT)


@pytest.fixture
def attempt_key(key_prefix: str) -> str:
    return key_prefix + _attempt_key(_TENANT, _NOTE_ID)


async def _take(
    client: redis_async.Redis,
    attempt_key: str,
    rate_key: str,
    *,
    cap: int = _ATTEMPT_CAP,
    limit: int = _RATE_LIMIT,
    window: int = _WINDOW,
) -> list[int]:
    """One raw execution of the imported script: [outcome, attempts, rate count]."""
    return await client.eval(
        _TAKE_PROMPT_SLOTS_SCRIPT,
        2,
        attempt_key,
        rate_key,
        cap,
        _ATTEMPT_TTL,
        limit,
        window,
    )


async def test_the_rate_slots_run_out_across_notes_with_the_count_before(
    real_redis: redis_async.Redis, key_prefix: str, rate_key: str
) -> None:
    """Counterpart: test_the_rate_slots_run_out_across_notes."""
    notes = [key_prefix + _attempt_key(_TENANT, n) for n in (10, 11, 12, 13)]
    replies = [await _take(real_redis, note, rate_key) for note in notes]

    assert replies == [
        [_SLOTS_ALLOWED, 1, 0],
        [_SLOTS_ALLOWED, 1, 1],
        [_SLOTS_ALLOWED, 1, 2],
        [_SLOTS_DENIED_BY_RATE, 0, _RATE_LIMIT],
    ]
    assert await real_redis.get(rate_key) == str(_RATE_LIMIT)
    assert await real_redis.exists(notes[-1]) == 0


async def test_the_attempt_cap_denies_and_leaves_the_rate_key_untouched(
    real_redis: redis_async.Redis, attempt_key: str, rate_key: str
) -> None:
    """Counterpart: the same name."""
    await real_redis.set(attempt_key, _ATTEMPT_CAP)

    assert await _take(real_redis, attempt_key, rate_key) == [
        _SLOTS_DENIED_BY_ATTEMPT,
        _ATTEMPT_CAP,
        0,
    ]
    assert await real_redis.exists(rate_key) == 0
    assert await real_redis.ttl(attempt_key) == _TTL_NO_EXPIRY


async def test_the_rate_limit_denies_and_leaves_the_attempt_key_untouched(
    real_redis: redis_async.Redis, attempt_key: str, rate_key: str
) -> None:
    """Counterpart: the same name."""
    await real_redis.set(rate_key, _RATE_LIMIT)

    assert await _take(real_redis, attempt_key, rate_key) == [
        _SLOTS_DENIED_BY_RATE,
        0,
        _RATE_LIMIT,
    ]
    assert await real_redis.exists(attempt_key) == 0
    assert await real_redis.ttl(rate_key) == _TTL_NO_EXPIRY


async def test_both_windows_are_set_on_create_and_only_on_create(
    real_redis: redis_async.Redis, attempt_key: str, rate_key: str
) -> None:
    """Counterparts: test_both_windows_are_set_when_the_counters_are_created,
    test_a_later_take_does_not_refresh_either_window."""
    await _take(real_redis, attempt_key, rate_key, cap=2)
    _assert_window(await real_redis.ttl(attempt_key), _ATTEMPT_TTL)
    _assert_window(await real_redis.ttl(rate_key))

    await _take(real_redis, attempt_key, rate_key, cap=2, window=_WINDOW * 100)
    assert await real_redis.ttl(attempt_key) <= _ATTEMPT_TTL
    assert await real_redis.ttl(rate_key) <= _WINDOW


async def test_keys_without_a_ttl_gain_one_on_the_next_take(
    real_redis: redis_async.Redis, attempt_key: str, rate_key: str
) -> None:
    """Counterpart: the same name. Audit M4, repaired for both keys."""
    await real_redis.set(attempt_key, 1)
    await real_redis.set(rate_key, 1)

    await _take(real_redis, attempt_key, rate_key, cap=2)

    _assert_window(await real_redis.ttl(attempt_key), _ATTEMPT_TTL)
    _assert_window(await real_redis.ttl(rate_key))


async def test_concurrent_slots_are_taken_exactly_up_to_the_limit(
    real_redis: redis_async.Redis, key_prefix: str, rate_key: str
) -> None:
    """No hermetic counterpart: fakeredis runs in-process, so nothing there is
    ever concurrent. Here the gathered EVALs, each on its own note, reach one
    server at once -- register item 27's claim that two judgements cannot both
    take the last slot.

    Exact, not "at most": every allowed reply saw a DIFFERENT count before it, and
    every refused one saw the full count.
    """
    limit = 10
    replies = await asyncio.gather(
        *(
            _take(
                real_redis,
                key_prefix + _attempt_key(_TENANT, note_id),
                rate_key,
                limit=limit,
            )
            for note_id in range(limit + 5)
        )
    )

    allowed = sorted(rate for outcome, _, rate in replies if outcome == _SLOTS_ALLOWED)
    refused = [rate for outcome, _, rate in replies if outcome == _SLOTS_DENIED_BY_RATE]
    assert allowed == list(range(limit))
    assert refused == [limit] * 5
    assert await real_redis.get(rate_key) == str(limit)


async def test_concurrent_takes_on_one_note_have_exactly_one_winner(
    real_redis: redis_async.Redis, attempt_key: str, rate_key: str
) -> None:
    """No hermetic counterpart. Register item 119's claim: ten takes for one note
    reaching the server at once send one question, and the other nine read the cap
    back without taking a rate slot."""
    replies = await asyncio.gather(
        *(_take(real_redis, attempt_key, rate_key) for _ in range(10))
    )

    outcomes = [outcome for outcome, _, _ in replies]
    assert outcomes.count(_SLOTS_ALLOWED) == 1
    assert outcomes.count(_SLOTS_DENIED_BY_ATTEMPT) == 9
    assert await real_redis.mget(attempt_key, rate_key) == ["1", "1"]


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


async def test_the_take_over_replaces_only_the_value_it_read(
    real_redis: redis_async.Redis, reservation_key: str
) -> None:
    """Counterparts: the take-over tests in test_cost_lua.py. A changed value is
    left alone; the value read becomes the reservation with the short TTL."""
    await real_redis.set(reservation_key, "stale", ex=_LONG_TTL)

    refused = await real_redis.eval(
        _TAKE_OVER_SCRIPT, 1, reservation_key, "other", _RESERVED, _SHORT_TTL
    )
    assert int(refused) == 0
    assert await real_redis.get(reservation_key) == "stale"

    taken = await real_redis.eval(
        _TAKE_OVER_SCRIPT, 1, reservation_key, "stale", _RESERVED, _SHORT_TTL
    )
    assert int(taken) == 1
    assert await real_redis.get(reservation_key) == _RESERVED
    _assert_window(await real_redis.ttl(reservation_key), _SHORT_TTL)


async def test_concurrent_take_overs_have_exactly_one_winner(
    real_redis: redis_async.Redis, reservation_key: str
) -> None:
    """Ten duplicates that read the same invalid value: one takes it over."""
    await real_redis.set(reservation_key, "stale", ex=_LONG_TTL)
    results = await asyncio.gather(
        *(
            real_redis.eval(
                _TAKE_OVER_SCRIPT, 1, reservation_key, "stale", _RESERVED, _SHORT_TTL
            )
            for _ in range(10)
        )
    )
    assert sorted(int(r) for r in results) == [0] * 9 + [1]


async def test_a_release_frees_the_note_for_a_new_reservation(
    real_redis: redis_async.Redis, reservation_key: str
) -> None:
    """Counterpart: test_release_removes_the_reservation."""
    await real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)

    assert await real_redis.delete(reservation_key) == 1

    assert await real_redis.exists(reservation_key) == 0
    reclaim = await real_redis.set(reservation_key, _RESERVED, nx=True, ex=_SHORT_TTL)
    assert reclaim is True


# --- (e) one question per note, text A then text B (register item 120) -----

# A judgement that asks: a total of 69 accepts with a flag, and the note is vague.
_CONFIG = get_tenant_config(_TENANT)
_SCORE = NoteScore(total=69, band=_CONFIG.band_for(69), denominator=80, components=[])
_ANALYSIS = NoteAnalysis(
    note_type=NoteType.DISCOVERY,
    is_vague=True,
    missing_components=[],
    clarification_prompt="Which day is the follow-up?",
    reasoning="...",
)


async def _judge_prompt(
    client: redis_async.Redis, attempt_key: str, rate_key: str
) -> Decision:
    """One judgement's prompt decision, in the pipeline's order: step 5 reads the
    attempts, a provisional decide picks the trip, step 8 takes both slots only
    when a prompt would be sent, and decide runs again on what the store said."""
    read = int(await client.get(attempt_key) or 0)

    def _decide(attempts: int, rate_allowed: bool, rate_count: int) -> Decision:
        return decide(
            _SCORE,
            _ANALYSIS,
            attempts=attempts,
            rate_allowed=rate_allowed,
            rate_count=rate_count,
            config=_CONFIG,
            resubmission=False,
        )

    if not _decide(read, True, 0).prompt_sent:
        return _decide(read, True, 0)
    reply = await client.eval(
        _TAKE_PROMPT_SLOTS_SCRIPT,
        2,
        attempt_key,
        rate_key,
        _CONFIG.clarification_cap,
        _CONFIG.attempt_ttl_seconds,
        _CONFIG.rate_limit_per_hour,
        _CONFIG.rate_limit_window_seconds,
    )
    return _decide(*_prompt_slots_answer(reply))


async def test_text_a_then_text_b_on_one_note_withholds_the_second_at_the_cap(
    real_redis: redis_async.Redis, key_prefix: str, attempt_key: str, rate_key: str
) -> None:
    """No hermetic counterpart at this depth. Text B is a new fingerprint, so its
    reservation is claimed rather than refused, and with no flush between the two
    the second judgement withholds at attempt_cap with the counter still at 1."""
    decisions = []
    # Two inert digests stand for text A and text B; neither is derived from a note.
    for digest in ("a" * 64, "b" * 64):
        reservation = key_prefix + _idempotency_key(_TENANT, _NOTE_ID, digest)
        claimed = await real_redis.set(reservation, _RESERVED, nx=True, ex=_SHORT_TTL)
        assert claimed is True
        decisions.append(await _judge_prompt(real_redis, attempt_key, rate_key))

    assert [d.prompt_sent for d in decisions] == [True, False]
    assert decisions[1].prompt_withheld is PromptWithheld.ATTEMPT_CAP
    assert decisions[1].attempt == 1
    assert await real_redis.mget(attempt_key, rate_key) == ["1", "1"]
    _assert_window(await real_redis.ttl(attempt_key), _CONFIG.attempt_ttl_seconds)
