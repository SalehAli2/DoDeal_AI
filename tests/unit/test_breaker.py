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


async def test_a_non_redis_error_is_not_counted(breaker) -> None:
    """A bug in our own code is not evidence about the store."""

    async def _boom() -> None:
        raise ValueError("ours, not theirs")

    for _ in range(THRESHOLD * 2):
        with pytest.raises(ValueError):
            await breaker.call(_boom)

    assert breaker.state is BreakerState.CLOSED


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
