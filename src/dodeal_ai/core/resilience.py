"""Watchdog — one shared wrapper for every EXTERNAL call (LLM + backend tools).

Every call out of this service (to the model, or later to the Hikal API) runs
under an explicit timeout and retries at most ONCE on failure/timeout, then
fails closed with a clear error. This policy is defined HERE, once, so no call
site invents its own timeout/retry behaviour.

Nothing calls the LLM or tools yet — this is the reusable wrapper, ready to wrap
those calls when they exist. Built and tested in isolation.

IDEMPOTENCY NOTE: retry-once is safe for READS. For a non-idempotent WRITE (the
future note-writeback) a blind retry could double-execute. Callers wrapping a
write must pass retry=False or make the operation idempotent. See ASSUMPTIONS.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from dodeal_ai.core.config import get_settings

_logger = logging.getLogger("dodeal_ai.resilience")


class ExternalCallError(Exception):
    """An external call failed after its timeout and (optional) single retry.
    Fail closed: raise this rather than returning partial/absent data that
    downstream code might mistake for a real result. Carries a short label
    naming which call failed (for logs, not for clients)."""

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
            _logger.warning(
                "external_call_failed label=%s attempt=%d of=%d error=%r",
                label,
                attempt,
                attempts,
                exc,
            )
            # loop continues to the retry if attempts remain

    # All attempts exhausted -> fail closed.
    raise ExternalCallError(label, last_exc)  # type: ignore[arg-type]
