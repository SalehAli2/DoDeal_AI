"""The circuit breaker's state machine, driven by a fake clock the test owns."""

from __future__ import annotations

import asyncio
import logging

import pytest
import redis

from dodeal_ai.core.breaker import (
    BreakerOpen,
    BreakerState,
    CircuitBreaker,
    breaker_field,
    cost_breaker,
    operational_breaker,
    reset_breakers,
)
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.redis import PoolExhausted

THRESHOLD = 3
OPEN_SECONDS = 30.0


class _Clock:
    """A monotonic reading the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Store:
    """A call that answers or fails like a dead server, and counts its own invocations."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.fail:
            raise redis.RedisError("down")
        return "answered"


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def breaker(clock: _Clock) -> CircuitBreaker:
    return CircuitBreaker(
        "test",
        failure_threshold=THRESHOLD,
        open_seconds=OPEN_SECONDS,
        clock=clock,
    )


async def _fail(breaker: CircuitBreaker, store: _Store, times: int = 1) -> None:
    for _ in range(times):
        with pytest.raises(redis.RedisError):
            await breaker.call(store)


# --- closed -----------------------------------------------------------------


async def test_a_closed_breaker_passes_the_call_through(breaker) -> None:
    store = _Store()

    assert await breaker.call(store) == "answered"
    assert (store.calls, breaker.state) == (1, BreakerState.CLOSED)


async def test_a_failure_under_the_threshold_leaves_it_closed(breaker) -> None:
    await _fail(breaker, _Store(fail=True), THRESHOLD - 1)

    assert breaker.state is BreakerState.CLOSED


async def test_the_failure_is_re_raised_unchanged(breaker) -> None:
    """A counted failure is re-raised as the same object, never wrapped."""
    original = redis.RedisError("down")

    async def _raise() -> None:
        raise original

    with pytest.raises(redis.RedisError) as caught:
        await breaker.call(_raise)

    assert caught.value is original


async def test_a_success_resets_the_count(breaker) -> None:
    """Failures count consecutively, so one success in between clears them."""
    await _fail(breaker, _Store(fail=True), THRESHOLD - 1)
    await breaker.call(_Store())
    await _fail(breaker, _Store(fail=True), THRESHOLD - 1)

    assert breaker.state is BreakerState.CLOSED


async def _boom() -> None:
    raise ValueError("ours, not theirs")


async def test_a_non_redis_error_is_not_counted(breaker) -> None:
    """A bug in our own code is not evidence about the store."""
    for _ in range(THRESHOLD * 2):
        with pytest.raises(ValueError):
            await breaker.call(_boom)

    assert breaker.state is BreakerState.CLOSED


async def test_a_non_redis_error_neither_counts_nor_clears_the_count(breaker) -> None:
    """Item 80 (d): in CLOSED our own error leaves the consecutive count exactly
    where it was -- one more store failure still opens it, and no fewer would."""
    await _fail(breaker, _Store(fail=True), THRESHOLD - 1)

    with pytest.raises(ValueError):
        await breaker.call(_boom)
    assert breaker.state is BreakerState.CLOSED  # not counted...

    await _fail(breaker, _Store(fail=True))
    assert breaker.state is BreakerState.OPEN  # ...and not cleared either


# --- pool exhaustion is not a store failure (register item 81) --------------

# The default threshold, so "five of them" reads the way the incident would.
DEFAULT_THRESHOLD = 5


@pytest.fixture
def breaker_at_five(clock: _Clock) -> CircuitBreaker:
    return CircuitBreaker(
        "test",
        failure_threshold=DEFAULT_THRESHOLD,
        open_seconds=OPEN_SECONDS,
        clock=clock,
    )


async def test_pool_exhaustion_is_not_counted(breaker_at_five) -> None:
    """Five refusals from our OWN pool leave it CLOSED with the count at zero.
    The store was never asked, so a latency spike that queues requests on the
    pool must not become 30s of 503s on a healthy Redis."""

    async def _exhausted() -> None:
        raise PoolExhausted()

    for _ in range(DEFAULT_THRESHOLD):
        with pytest.raises(PoolExhausted):
            await breaker_at_five.call(_exhausted)
    assert breaker_at_five.state is BreakerState.CLOSED

    # The count really is zero: it still takes the whole threshold to open it.
    await _fail(breaker_at_five, _Store(fail=True), DEFAULT_THRESHOLD - 1)
    assert breaker_at_five.state is BreakerState.CLOSED
    await _fail(breaker_at_five, _Store(fail=True))
    assert breaker_at_five.state is BreakerState.OPEN


async def test_a_refused_connection_is_counted(breaker_at_five) -> None:
    """The contrast. A ConnectionError from the socket IS the store talking, and
    five of them open it -- only the pool's own refusal is excluded."""

    async def _refused() -> None:
        raise redis.ConnectionError("refused")

    for _ in range(DEFAULT_THRESHOLD):
        with pytest.raises(redis.ConnectionError):
            await breaker_at_five.call(_refused)

    assert breaker_at_five.state is BreakerState.OPEN


# --- open -------------------------------------------------------------------


async def test_the_threshold_opens_it(breaker) -> None:
    await _fail(breaker, _Store(fail=True), THRESHOLD)

    assert breaker.state is BreakerState.OPEN


async def test_an_open_breaker_refuses_without_calling(breaker) -> None:
    """The point of the whole thing: no socket, no timeout, no wait."""
    await _fail(breaker, _Store(fail=True), THRESHOLD)
    store = _Store()

    with pytest.raises(BreakerOpen):
        await breaker.call(store)

    assert store.calls == 0


async def test_it_stays_open_for_the_whole_window(breaker, clock) -> None:
    await _fail(breaker, _Store(fail=True), THRESHOLD)
    clock.advance(OPEN_SECONDS - 0.001)
    store = _Store()

    with pytest.raises(BreakerOpen):
        await breaker.call(store)

    assert store.calls == 0


# --- half-open: exactly one probe ------------------------------------------


async def test_one_probe_is_admitted_after_the_window(breaker, clock) -> None:
    await _fail(breaker, _Store(fail=True), THRESHOLD)
    clock.advance(OPEN_SECONDS)
    probe = _Store()

    assert await breaker.call(probe) == "answered"
    assert probe.calls == 1


async def test_only_one_probe_is_admitted(breaker, clock) -> None:
    """While the one probe is still in flight, every other caller is refused."""
    await _fail(breaker, _Store(fail=True), THRESHOLD)
    clock.advance(OPEN_SECONDS)
    answered = asyncio.Event()
    probe_calls = 0

    async def _slow_probe() -> str:
        nonlocal probe_calls
        probe_calls += 1
        await answered.wait()
        return "answered"

    probe = asyncio.create_task(breaker.call(_slow_probe))
    await asyncio.sleep(0)  # let the probe be admitted, then suspend

    others = _Store()
    for _ in range(3):
        with pytest.raises(BreakerOpen):
            await breaker.call(others)

    assert (probe_calls, others.calls) == (1, 0)
    answered.set()
    assert await probe == "answered"


async def test_a_successful_probe_closes_it(breaker, clock) -> None:
    await _fail(breaker, _Store(fail=True), THRESHOLD)
    clock.advance(OPEN_SECONDS)

    await breaker.call(_Store())

    assert breaker.state is BreakerState.CLOSED
    assert await breaker.call(_Store()) == "answered"


async def test_a_failed_probe_reopens_it_for_a_full_window(breaker, clock) -> None:
    """A failed probe reopens it at once, without counting to the threshold again."""
    await _fail(breaker, _Store(fail=True), THRESHOLD)
    clock.advance(OPEN_SECONDS)
    await _fail(breaker, _Store(fail=True))

    assert breaker.state is BreakerState.OPEN
    clock.advance(OPEN_SECONDS - 0.001)
    with pytest.raises(BreakerOpen):
        await breaker.call(_Store())


# --- a probe that does not answer (register item 80) ------------------------


async def _open_past_the_window(breaker: CircuitBreaker, clock: _Clock) -> None:
    """Open it with real failures, then let the window run out: the next call is
    the probe."""
    await _fail(breaker, _Store(fail=True), THRESHOLD)
    clock.advance(OPEN_SECONDS)


async def _assert_re_armed_for_a_full_window(
    breaker: CircuitBreaker, clock: _Clock
) -> None:
    """OPEN again, refusing for a whole window measured from NOW -- so the
    opening was re-stamped rather than left at the first one, which is long past
    -- and then admitting a probe whose success closes it."""
    assert breaker.state is BreakerState.OPEN
    clock.advance(OPEN_SECONDS - 0.001)
    refused = _Store()
    with pytest.raises(BreakerOpen):
        await breaker.call(refused)
    assert refused.calls == 0

    clock.advance(0.001)
    probe = _Store()
    assert await breaker.call(probe) == "answered"
    assert (probe.calls, breaker.state) == (1, BreakerState.CLOSED)


async def test_a_probe_that_raises_our_own_error_re_arms_the_window(
    breaker, clock, caplog
) -> None:
    """Item 80 (a). A probe that failed in our code said nothing about the store,
    and HALF_OPEN with no probe out would refuse every call from here on."""
    await _open_past_the_window(breaker, clock)
    caplog.clear()  # the opening itself is not this test's line

    with (
        caplog.at_level(logging.WARNING, logger="dodeal_ai.breaker"),
        pytest.raises(ValueError),
    ):
        await breaker.call(_boom)

    assert [r.getMessage() for r in caplog.records] == [
        "breaker_probe_abandoned",
        "breaker_opened",
    ]
    await _assert_re_armed_for_a_full_window(breaker, clock)


async def test_a_cancelled_probe_re_arms_the_window(breaker, clock, caplog) -> None:
    """Item 80 (b). The deadline, a disconnect and a shutdown all arrive as a
    CancelledError -- a BaseException, which a RedisError branch never sees."""
    await _open_past_the_window(breaker, clock)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def _hung() -> None:
        entered.set()
        await never.wait()

    probe = asyncio.create_task(breaker.call(_hung))
    await entered.wait()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.breaker"):
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe

    assert [r.getMessage() for r in caplog.records] == [
        "breaker_probe_abandoned",
        "breaker_opened",
    ]
    assert {r.breaker for r in caplog.records} == {"test"}
    await _assert_re_armed_for_a_full_window(breaker, clock)


async def test_a_probe_that_never_returns_is_taken_over_after_a_window(
    breaker, clock, caplog
) -> None:
    """Item 80 (c). A probe task that is never resumed raises nothing, so only
    its age can free the breaker: once it is a window old, the next call is the
    probe, and that probe's success closes it."""
    await _open_past_the_window(breaker, clock)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def _hung() -> None:
        entered.set()
        await never.wait()

    hung = asyncio.create_task(breaker.call(_hung))
    await entered.wait()

    # Still inside the hung probe's own window: everyone else is refused.
    clock.advance(OPEN_SECONDS - 0.001)
    refused = _Store()
    with pytest.raises(BreakerOpen):
        await breaker.call(refused)
    assert refused.calls == 0

    clock.advance(0.001)
    takeover = _Store()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.breaker"):
        assert await breaker.call(takeover) == "answered"

    assert (takeover.calls, breaker.state) == (1, BreakerState.CLOSED)
    assert [r.getMessage() for r in caplog.records] == [
        "breaker_probe_abandoned",
        "breaker_closed",
    ]

    # The abandoned task finally ends. The breaker is CLOSED by now, so its
    # cancellation neither counts nor reopens anything.
    hung.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hung
    assert breaker.state is BreakerState.CLOSED


# --- a probe the pool refused (register item 95) ----------------------------


async def _exhausted() -> None:
    raise PoolExhausted()


async def test_a_pool_refused_probe_reopens_without_a_new_window(
    breaker, clock, caplog
) -> None:
    """Item 95. The probe never reached the store, so the slot goes back at once
    and the elapsed window is left elapsed -- not re-stamped from the clock."""
    await _open_past_the_window(breaker, clock)
    opened_at = breaker._opened_at
    caplog.clear()

    with (
        caplog.at_level(logging.WARNING, logger="dodeal_ai.breaker"),
        pytest.raises(PoolExhausted) as caught,
    ):
        await breaker.call(_exhausted)

    assert breaker.state is BreakerState.OPEN
    assert breaker._opened_at == opened_at
    assert type(caught.value) is PoolExhausted
    # The existing line, not a new event name: a refused probe did not answer.
    assert [r.getMessage() for r in caplog.records] == ["breaker_probe_abandoned"]
    assert {r.breaker for r in caplog.records} == {"test"}


async def test_the_next_call_after_a_pool_refused_probe_is_the_probe(
    breaker, clock
) -> None:
    """Item 95, the point of it. No clock advance at all: the very next caller
    reaches the store, and its success closes the breaker."""
    await _open_past_the_window(breaker, clock)
    with pytest.raises(PoolExhausted):
        await breaker.call(_exhausted)

    probe = _Store()
    assert await breaker.call(probe) == "answered"

    assert (probe.calls, breaker.state) == (1, BreakerState.CLOSED)


async def test_a_pool_refusal_while_closed_still_changes_nothing(
    breaker, clock, caplog
) -> None:
    """The fix must not widen. In CLOSED a pool refusal is still uncounted, still
    silent, and still leaves the state and the failure count where they were."""
    await _fail(breaker, _Store(fail=True), THRESHOLD - 1)
    caplog.clear()

    with (
        caplog.at_level(logging.WARNING, logger="dodeal_ai.breaker"),
        pytest.raises(PoolExhausted),
    ):
        await breaker.call(_exhausted)

    assert breaker.state is BreakerState.CLOSED
    assert caplog.records == []
    # The count was neither raised nor cleared: one more store failure opens it.
    await _fail(breaker, _Store(fail=True))
    assert breaker.state is BreakerState.OPEN


# --- the two log lines ------------------------------------------------------


async def test_each_transition_logs_exactly_once(breaker, clock, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.breaker"):
        await _fail(breaker, _Store(fail=True), THRESHOLD)
        # Refusals while open are silent: the transition already said it.
        for _ in range(3):
            with pytest.raises(BreakerOpen):
                await breaker.call(_Store())
        clock.advance(OPEN_SECONDS)
        await breaker.call(_Store())

    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["breaker_opened", "breaker_closed"]
    assert {r.levelname for r in caplog.records} == {"WARNING"}
    assert {r.breaker for r in caplog.records} == {"test"}


# --- the integration with every existing except clause ----------------------


def test_breaker_open_is_a_redis_error() -> None:
    """The one property every fail-open and fail-closed path depends on."""
    assert issubclass(BreakerOpen, redis.RedisError)
    assert isinstance(BreakerOpen("cost"), redis.RedisError)


def test_the_breaker_field_names_a_refusal_and_nothing_else() -> None:
    """The field marks a refused call and never a store that was asked and failed."""
    assert breaker_field(BreakerOpen("cost")) == {"breaker": "open"}
    assert breaker_field(redis.RedisError("down")) == {}


# --- the two module-level breakers -----------------------------------------


def test_the_two_breakers_are_separate_and_cached() -> None:
    """Each connection has its own cached breaker, so a dead cost store cannot refuse
    the reservation."""
    assert cost_breaker() is cost_breaker()
    assert cost_breaker() is not operational_breaker()
    assert (cost_breaker().name, operational_breaker().name) == ("cost", "operational")


async def test_they_are_built_from_settings(monkeypatch) -> None:
    """The threshold is read from Settings when the breaker is built."""
    monkeypatch.setenv("DODEAL_BREAKER_FAILURE_THRESHOLD", "2")
    get_settings.cache_clear()
    reset_breakers()

    await _fail(cost_breaker(), _Store(fail=True), 2)

    assert cost_breaker().state is BreakerState.OPEN


async def test_reset_returns_a_breaker_to_closed(breaker) -> None:
    await _fail(breaker, _Store(fail=True), THRESHOLD)

    breaker.reset()

    assert breaker.state is BreakerState.CLOSED
    assert await breaker.call(_Store()) == "answered"


def test_reset_breakers_drops_the_cached_pair() -> None:
    """After a reset the next call builds a fresh breaker instead of inheriting
    failures."""
    first = cost_breaker()

    reset_breakers()

    assert cost_breaker() is not first
