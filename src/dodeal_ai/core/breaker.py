"""Circuit breaker for the two Redis connections.

After `failure_threshold` consecutive failures a breaker OPENS and refuses calls
for `open_seconds` without touching a socket; then ONE probe decides whether it
closes or opens again.

BreakerOpen IS a redis.RedisError, so every existing RedisError handler keeps
its policy: the money guards fail open, the reservation still fails closed.
Redis only -- the provider half (A9) is not here.

A PROBE THAT DOES NOT ANSWER CANNOT WEDGE IT (register item 80). HALF_OPEN
refuses every caller but the probe, so a probe that never reports back would
refuse every call until the process restarted -- on db2, a 503 on every
judgement. Two guards, because there are two ways not to report back:

  1. The probe ENDS without an answer: it is cancelled, or it raises something
     that is not a RedisError. The window is re-armed, as if it had failed.
  2. The probe NEVER ends: its task is never resumed. `call` cannot see that,
     so `_admit` does -- a probe older than `open_seconds` is abandoned, and
     the arriving call is admitted as the probe in its place.

A cancelled probe is not COUNTED as a failure, only re-armed like one.
Cancellation is news about the caller -- a deadline, a disconnect, a shutdown
-- and never about the store; closing on it would be as wrong as opening on it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from functools import lru_cache

import redis

from dodeal_ai.core.config import get_settings

_logger = logging.getLogger("dodeal_ai.breaker")


class BreakerOpen(redis.RedisError):
    """The breaker refused this call without asking the store."""

    def __init__(self, name: str) -> None:
        self.breaker = name
        super().__init__(f"circuit breaker open: {name}")


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """One connection's breaker. No lock: every transition runs in synchronous
    code between awaits, so on one event loop no two calls interleave inside it."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int,
        open_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        # Monotonic, never wall time: a clock step must not hold a breaker open
        # for hours or reopen it early.
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        # When the current probe was admitted. Set on every entry to HALF_OPEN
        # and on every takeover, so HALF_OPEN is never without one.
        self._probe_started = 0.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BreakerState:
        """As recorded: an OPEN breaker whose window has passed reads OPEN until
        a call arrives to probe it."""
        return self._state

    async def call[T](self, factory: Callable[[], Awaitable[T]]) -> T:
        """Run `factory()` under the breaker, or raise BreakerOpen without calling it.

        Only a RedisError counts as a failure, and it is re-raised unchanged; a
        bug in our own code is not evidence about the store. Anything else, a
        cancellation included, is re-raised unchanged too and moves the breaker
        only when it ends a probe (guard 1 in the module docstring).
        """
        self._admit()
        try:
            result = await factory()
        except redis.RedisError:
            self._record_failure()
            raise
        except BaseException:
            # The probe did not answer the question (cancelled, or failed in
            # our own code). Not evidence either way: re-arm the window rather
            # than leave HALF_OPEN with nothing to move it.
            if self._state is BreakerState.HALF_OPEN:
                self._log_probe_abandoned()
                self._open()
            raise
        self._record_success()
        return result

    def reset(self) -> None:
        """Back to closed with no history."""
        self._state = BreakerState.CLOSED
        self._failures = 0

    def _admit(self) -> None:
        """Refuse, or let the call through. The first call after the window
        becomes the single probe by moving to HALF_OPEN, which refuses the rest
        -- until that probe is `open_seconds` old, when it is taken as abandoned
        and the arriving call becomes the probe instead (guard 2)."""
        if self._state is BreakerState.CLOSED:
            return
        now = self._clock()
        if self._state is BreakerState.OPEN:
            if now - self._opened_at < self._open_seconds:
                raise BreakerOpen(self._name)
            self._state = BreakerState.HALF_OPEN
        elif now - self._probe_started < self._open_seconds:
            # HALF_OPEN, and its probe is still inside the window.
            raise BreakerOpen(self._name)
        else:
            # HALF_OPEN, and its probe never came back. Stay HALF_OPEN: this
            # call is the probe now.
            self._log_probe_abandoned()
        self._probe_started = now

    def _record_failure(self) -> None:
        if self._state is BreakerState.HALF_OPEN:
            # The probe failed: straight back to open for another full window.
            self._open()
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._open()

    def _record_success(self) -> None:
        if self._state is BreakerState.HALF_OPEN:
            self._state = BreakerState.CLOSED
            self._failures = 0
            _logger.warning("breaker_closed", extra={"breaker": self._name})
            return
        # Consecutive failures only, so one success clears the count.
        self._failures = 0

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        _logger.warning("breaker_opened", extra={"breaker": self._name})

    def _log_probe_abandoned(self) -> None:
        """One line per probe that did not answer, on either guard's path."""
        _logger.warning("breaker_probe_abandoned", extra={"breaker": self._name})


def breaker_field(exc: BaseException) -> dict[str, str]:
    """`{"breaker": "open"}` when the breaker refused the call, `{}` when the store
    was asked and failed -- merged into the existing bypass lines."""
    return {"breaker": "open"} if isinstance(exc, BreakerOpen) else {}


def _from_settings(name: str) -> CircuitBreaker:
    settings = get_settings()
    return CircuitBreaker(
        name,
        failure_threshold=settings.breaker_failure_threshold,
        open_seconds=settings.breaker_open_seconds,
    )


@lru_cache
def cost_breaker() -> CircuitBreaker:
    """The db1 breaker, built on first use and shared by every call site."""
    return _from_settings("cost")


@lru_cache
def operational_breaker() -> CircuitBreaker:
    """The db2 breaker. Separate from db1's, so a dead cost store cannot refuse
    the idempotency reservation."""
    return _from_settings("operational")


def reset_breakers() -> None:
    """Drop both cached breakers; the next use builds fresh ones from settings."""
    cost_breaker.cache_clear()
    operational_breaker.cache_clear()
