"""Watchdog: succeeds first try, succeeds on retry, fails closed, respects timeout.

And `gather_or_cancel`, which is the other thing this module owns: what happens
to the SIBLING when one of a concurrent pair fails. The claims worth testing are
not "the exception came out" -- gather does that -- but the three the docstring
makes: nothing is left running, the exception arrives unchanged, and a
CancelledError aimed at the caller is not swallowed.
"""

from __future__ import annotations

import asyncio

import pytest

from dodeal_ai.core import resilience
from dodeal_ai.core.resilience import (
    ExternalCallError,
    call_with_watchdog,
    gather_first_wins,
    gather_or_cancel,
)


async def test_succeeds_first_try():
    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        return "ok"

    result = await call_with_watchdog(op, label="test")
    assert result == "ok"
    assert calls == 1  # no retry needed


async def test_succeeds_on_retry():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first attempt fails")
        return "recovered"

    result = await call_with_watchdog(flaky, label="test", retry=True)
    assert result == "recovered"
    assert calls == 2  # failed once, succeeded on the single retry


async def test_fails_closed_after_retry():
    calls = 0

    async def always_fails():
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    with pytest.raises(ExternalCallError) as exc:
        await call_with_watchdog(always_fails, label="test", retry=True)
    assert exc.value.label == "test"
    assert isinstance(exc.value.cause, RuntimeError)
    assert calls == 2  # tried exactly twice, then failed closed


async def test_no_retry_tries_once():
    calls = 0

    async def always_fails():
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    with pytest.raises(ExternalCallError):
        await call_with_watchdog(always_fails, label="test", retry=False)
    assert calls == 1  # retry disabled -> single attempt


async def test_respects_timeout():
    async def too_slow():
        await asyncio.sleep(1.0)
        return "never"

    with pytest.raises(ExternalCallError) as exc:
        # 0.05s timeout, no retry -> times out fast and fails closed.
        await call_with_watchdog(too_slow, label="test", timeout=0.05, retry=False)
    assert isinstance(exc.value.cause, (asyncio.TimeoutError, TimeoutError))


# --- gather_or_cancel (register item 63) -----------------------------------


class _Watched:
    """A coroutine that records how far it got.

    `finished` is the flag that matters: it is set on the line AFTER the sleep,
    so it stays False for a task that was cancelled mid-sleep and True for one
    that was allowed to run to the end. "Nothing is left running" is not
    observable any other way -- a leaked task is still pending, and a test that
    only asserted the exception came out would pass with the leak in place.
    """

    def __init__(self, delay: float = 0.05, result: object = "ok"):
        self.delay = delay
        self.result = result
        self.started = False
        self.finished = False
        self.cancelled = False

    async def __call__(self) -> object:
        self.started = True
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        return self.result


async def _boom(exc: BaseException) -> None:
    raise exc


async def test_gather_returns_results_in_argument_order():
    async def first():
        # Deliberately the SLOWER of the two: if the results came back in
        # completion order this would be second, and the assert would catch it.
        await asyncio.sleep(0.02)
        return "first"

    async def second():
        return "second"

    assert await gather_or_cancel(first(), second()) == ("first", "second")


async def test_gather_of_nothing_is_an_empty_tuple():
    # asyncio.wait raises ValueError on an empty set; a call site that built its
    # list from a condition should not have to know that.
    assert await gather_or_cancel() == ()


async def test_a_failure_cancels_the_sibling_and_waits_for_it():
    slow = _Watched()

    with pytest.raises(RuntimeError):
        await gather_or_cancel(_boom(RuntimeError("down")), slow())

    assert slow.started
    assert slow.cancelled
    assert not slow.finished


async def test_the_sibling_is_awaited_not_merely_cancelled():
    """The await in _cancel_and_drain, on its own.

    `task.cancel()` only SCHEDULES a CancelledError; the coroutine has not run
    its except/finally when cancel() returns. This sibling records the moment it
    actually stops, and the assertion is made the instant gather_or_cancel
    returns control -- so it fails if the helper cancels without waiting.
    """
    stopped = []

    async def sibling():
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            stopped.append("stopped")
            raise

    with pytest.raises(RuntimeError):
        await gather_or_cancel(_boom(RuntimeError("down")), sibling())

    assert stopped == ["stopped"]  # already stopped, with no sleep of our own


async def test_the_first_exception_is_re_raised_unchanged():
    # The pipeline's release-on-error block re-raises whatever arrives, and the
    # route maps it by TYPE. A wrapper here would turn a 503 into a 500.
    original = ExternalCallError("llm.unit_a.vague", RuntimeError("down"))

    with pytest.raises(ExternalCallError) as exc:
        await gather_or_cancel(_boom(original), _Watched()())

    assert exc.value is original


async def test_either_side_may_be_the_one_that_fails():
    # The mirror: the failure is the SECOND argument, so "first" cannot mean
    # "the first coroutine passed" by accident.
    slow = _Watched()

    with pytest.raises(RuntimeError):
        await gather_or_cancel(slow(), _boom(RuntimeError("down")))

    assert slow.cancelled and not slow.finished


async def test_argument_order_decides_which_failure_is_raised():
    # Two failures available at once. Argument order is the only rule a call
    # site can predict, so it is the one asserted.
    with pytest.raises(RuntimeError, match="left"):
        await gather_or_cancel(
            _boom(RuntimeError("left")), _boom(RuntimeError("right"))
        )


async def test_caller_cancellation_propagates_and_takes_the_children_with_it():
    """Promise 3: a CancelledError aimed at the caller is never swallowed.

    A helper that caught CancelledError to do its cleanup and then returned
    normally would make a cancelled request look like a completed one -- and
    leave both model calls running. Both halves are asserted: the caller sees
    the cancellation, and neither child finished.
    """
    left, right = _Watched(delay=5.0), _Watched(delay=5.0)

    async def _run():
        await gather_or_cancel(left(), right())

    task = asyncio.create_task(_run())
    while not (left.started and right.started):
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert left.cancelled and right.cancelled
    assert not left.finished and not right.finished


# --- the retry rule and the deadline (register item 89) ----------------------


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Every wait the watchdog schedules, recorded instead of slept."""
    recorded: list[float] = []

    async def _record(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(resilience.asyncio, "sleep", _record)
    return recorded


async def test_a_rule_that_says_none_tries_once(sleeps):
    """A retry rule returning None fails closed after one attempt."""
    calls = 0

    async def always_fails():
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    with pytest.raises(ExternalCallError):
        await call_with_watchdog(
            always_fails, label="t", retry=True, retry_delay=lambda exc: None
        )
    assert calls == 1
    assert sleeps == []


async def test_the_rule_is_asked_with_the_failure_and_its_wait_is_slept(sleeps):
    """The rule sees the exception and its answer is the wait before the retry."""
    seen: list[Exception] = []
    failure = RuntimeError("once")

    async def flaky():
        if not seen:
            raise failure
        return "ok"

    def rule(exc: Exception) -> float:
        seen.append(exc)
        return 0.3

    assert (
        await call_with_watchdog(flaky, label="t", retry=True, retry_delay=rule) == "ok"
    )
    assert seen == [failure]
    assert sleeps == [0.3]


async def test_a_retry_that_would_end_past_the_deadline_is_skipped(sleeps):
    """The deadline is event-loop time; a wait reaching it means no retry."""
    calls = 0

    async def always_fails():
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    deadline = asyncio.get_running_loop().time() + 0.1
    with pytest.raises(ExternalCallError):
        await call_with_watchdog(
            always_fails,
            label="t",
            retry=True,
            retry_delay=lambda exc: 0.2,
            deadline=deadline,
        )
    assert calls == 1


async def test_no_rule_retries_at_once_without_a_sleep(sleeps):
    """The old behaviour: no rule means an immediate retry and no suspension."""
    calls = 0

    async def always_fails():
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    with pytest.raises(ExternalCallError):
        await call_with_watchdog(always_fails, label="t", retry=True)
    assert calls == 2
    assert sleeps == []


# --- gather_first_wins (register item 89) ------------------------------------


async def test_first_wins_returns_both_results_in_order():
    """On success the two results come back in argument order."""
    assert await gather_first_wins(_Watched(0.02, "a")(), _Watched(0, "b")()) == (
        "a",
        "b",
    )


async def test_the_first_error_wins_even_when_the_second_failed_earlier():
    """The second fails at once, the first fails later: the first's error is raised."""

    async def late_failure():
        await asyncio.sleep(0.02)
        raise LookupError("first")

    with pytest.raises(LookupError, match="first"):
        await gather_first_wins(late_failure(), _boom(RuntimeError("second")))


async def test_the_second_error_is_raised_when_the_first_succeeds():
    """A notes failure still surfaces once the lead has come back."""
    first = _Watched(0.02, "a")
    with pytest.raises(RuntimeError, match="second"):
        await gather_first_wins(first(), _boom(RuntimeError("second")))
    assert first.finished


async def test_a_first_failure_cancels_the_second():
    """The first's failure does not leave the second running."""
    slow = _Watched(delay=5.0)
    with pytest.raises(LookupError):
        await gather_first_wins(_boom(LookupError("first")), slow())
    assert slow.cancelled and not slow.finished


async def test_first_wins_leaves_no_unretrieved_exception():
    """The unraised second failure is retrieved, so asyncio reports nothing."""
    import gc

    reported: list[dict] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:

        async def late_failure():
            await asyncio.sleep(0.01)
            raise LookupError("first")

        with pytest.raises(LookupError):
            await gather_first_wins(late_failure(), _boom(RuntimeError("second")))
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert reported == []


async def test_first_wins_passes_caller_cancellation_to_both():
    """A cancelled caller cancels both reads and sees the cancellation."""
    left, right = _Watched(delay=5.0), _Watched(delay=5.0)
    task = asyncio.create_task(gather_first_wins(left(), right()))
    while not (left.started and right.started):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert left.cancelled and right.cancelled
