"""Watchdog — one shared wrapper for every EXTERNAL call (LLM + backend tools).

Every call out of this service (to the model, or to the CRM backend) runs
under an explicit timeout and retries at most ONCE on failure/timeout, then
fails closed with a clear error. This policy is defined HERE, once, so no call
site invents its own timeout/retry behaviour.

Nothing calls the LLM or tools yet — this is the reusable wrapper, ready to wrap
those calls when they exist. Built and tested in isolation.

IDEMPOTENCY NOTE: retry-once is safe for READS. For a non-idempotent call a
blind retry could double-execute; there is no write path today
(ASSUMPTIONS §3.1), the rule stands for the day one appears, and for paid calls
that may already have completed (LLM: retry=False). Callers wrapping such a
call must pass retry=False, or apply an idempotency key per FUTURE_PATTERNS.md
item 1 before enabling retry. See ASSUMPTIONS.md.

CONCURRENCY LIVES HERE TOO, for the same reason: `gather_or_cancel` is the one
place that says what happens to the OTHER call when one of a pair fails. A
call site that wrote its own would be writing a leak.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, overload

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.log_safety import safe_error_fields

_logger = logging.getLogger("dodeal_ai.resilience")


class ExternalCallError(Exception):
    """An external call failed after its timeout and (optional) single retry.
    Fail closed: raise this rather than returning partial/absent data that
    downstream code might mistake for a real result. Carries a short label
    naming which call failed (for logs, not for clients).

    `.cause` keeps the original exception for CALLERS to branch on (transient
    vs not). It is deliberately never logged with %r or str(): a foreign
    exception's message can quote the payload. Log it with
    core/log_safety.safe_error_fields instead."""

    def __init__(self, label: str, cause: Exception):
        self.label = label
        self.cause = cause
        super().__init__(f"external call failed: {label}")


async def call_with_watchdog[T](
    operation: Callable[[], Awaitable[T]],
    *,
    label: str,
    timeout: float | None = None,
    retry: bool | None = None,
) -> T:
    """Run `operation()` under a timeout, retrying once on failure/timeout.

    - operation: a zero-arg async callable producing the result. Passed as a
      callable (not an awaited coroutine) so we can invoke it a SECOND time for
      the retry — a coroutine object can only be awaited once.
    - label: short name for logs/errors (e.g. "llm.generate", "tool.get_lead").
    - timeout / retry: override config when given; else read from settings.

    Raises ExternalCallError if the operation fails after its allowed attempts.
    """
    settings = get_settings()
    timeout = settings.external_call_timeout_seconds if timeout is None else timeout
    retry = settings.external_call_retry_once if retry is None else retry

    attempts = 2 if retry else 1
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(operation(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - resilience boundary: ANY failure must fail closed uniformly
            last_exc = exc
            # Type, not message: `exc` here is whatever the external call
            # raised, and a foreign exception's message can quote the payload
            # that failed. See core/log_safety.py.
            _logger.warning(
                "external_call_failed",
                extra={
                    "label": label,
                    "attempt": attempt,
                    "of": attempts,
                    **safe_error_fields(exc),
                },
            )
            # loop continues to the retry if attempts remain

    # All attempts exhausted -> fail closed.
    raise ExternalCallError(label, last_exc)  # type: ignore[arg-type]


# --- concurrency: one failure must not leave its sibling running -----------


def _first_exception(tasks: Sequence[asyncio.Task[Any]]) -> BaseException | None:
    """The failure to re-raise, chosen in ARGUMENT order.

    `FIRST_EXCEPTION` returns as soon as one task raises, so in practice exactly
    one task is finished-with-an-exception when this is called. It is not
    guaranteed: two tasks can complete in the same loop iteration, and then
    "first" has to mean something. Argument order is the only choice a caller
    can predict from the call site, so that is the one taken.

    A task that was CANCELLED is skipped rather than reported: `.exception()`
    would raise on it, and a cancellation is not this pair's failure to report.
    Nothing here cancels a task before this runs, so reaching that case means
    somebody outside did -- and `.result()` on the success path will then raise
    the CancelledError, which is the truthful outcome.
    """
    for task in tasks:
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                return exc
    return None


async def _cancel_and_drain(tasks: Sequence[asyncio.Task[Any]]) -> None:
    """Cancel every unfinished task and WAIT for each one to actually stop.

    The await is the point. `task.cancel()` only schedules a CancelledError
    into the coroutine; it does not stop it, and without the await this
    function would return while the sibling was still inside an HTTP call --
    which is a leak that shows up as a paid model call for a request that
    already 503'd.

    `return_exceptions=True` because every task here is expected to end in a
    CancelledError and that is not news. Nothing else can be hiding in it: a
    task that had already failed is `done()` and is not in this list.

    Returns without awaiting anything at all when nothing is pending, so the
    success path gains no suspension point it did not have before.
    """
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@overload
async def gather_or_cancel[T1, T2](
    first: Awaitable[T1], second: Awaitable[T2], /
) -> tuple[T1, T2]: ...


@overload
async def gather_or_cancel(*coros: Awaitable[Any]) -> tuple[Any, ...]: ...


async def gather_or_cancel(*coros: Awaitable[Any]) -> tuple[Any, ...]:
    """Run `coros` concurrently. If one fails, CANCEL the others and re-raise.

    `asyncio.gather(..., return_exceptions=False)` propagates the first
    exception and LEAVES ITS SIBLINGS RUNNING. For two model calls that is a
    call nobody is waiting for any more: the request has already failed, and the
    abandoned pass carries on -- including, if its answer comes back malformed,
    into a reprompt that spends a second call on a judgement that will never be
    returned. This does what gather does on the happy path and closes that door
    on the failure path (register item 63).

    THE THREE PROMISES, in the order they matter:

      1. Nothing is left running. Every unfinished task is cancelled AND
         awaited, in a `finally`, so it holds on the failure path, on the
         caller-cancellation path, and on any path a future edit invents.
      2. The first exception is re-raised UNCHANGED -- the same object, not a
         wrapper. `ModelUnavailableError` must still arrive at the pipeline's
         release-on-error block as itself, or a 503 becomes a 500.
      3. A CancelledError aimed at the CALLER is never swallowed. The siblings
         are cancelled and drained on the way out and the CancelledError
         continues, so a cancelled request cancels the work it started rather
         than detaching from it.

    On success the results come back in ARGUMENT order, exactly as gather's do,
    so this is a drop-in replacement at a call site that unpacks them.

    Generic in the two-argument form because that is what both callers need --
    vague + score, and the lead + notes fetches (register item 9).
    The variadic overload keeps a third caller from being a signature change,
    at the cost of `Any` results it will have to narrow itself.
    """
    if not coros:
        # asyncio.wait raises ValueError on an empty set. Nothing to gather is
        # not an error at a call site that built its list from a condition.
        return ()

    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        failure = _first_exception(tasks)
        if failure is not None:
            raise failure
        return tuple(task.result() for task in tasks)
    finally:
        await _cancel_and_drain(tasks)
