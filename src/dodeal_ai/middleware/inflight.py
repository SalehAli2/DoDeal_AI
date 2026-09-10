"""Load shedding — refuse at the door rather than queue without a bound.

Register item 73. Every request that gets past this middleware is holding an
event-loop slot and, once it reaches Unit A, a reservation and up to three paid
model calls. Nothing counted them. A burst larger than the service can serve
does not fail visibly under asyncio; it QUEUES — the loop accepts every
connection, each one waits behind work it cannot get to, and the first symptom
is every caller timing out at once, including the callers whose requests would
have been served fine.

So there is a ceiling, and the request that meets it is refused IMMEDIATELY.
The refusal has to be cheaper than the work it refuses or it is not a defence,
which is what decides everything below:

  WHERE IT SITS. After RequestIDMiddleware and before everything else. After,
  so a refused caller still gets an id to quote in a support ticket and the
  WARNING line can be correlated with their retry. Before everything else, so a
  refusal costs a counter comparison and nothing more -- no token verification,
  no tenant resolution, no body read, no Redis round-trip.

  (Starlette's `add_middleware` INSERTS AT THE FRONT, and the front of that
  list is the OUTERMOST layer. So "after the request id" means registered
  BEFORE it in main.py. Getting that backwards is silent: the refusal keeps
  working and quietly loses its request id.)

  WHAT IT LOGS. The id and the count. Never a tenant -- the gates have not run,
  so the only tenant available would be the caller's unverified claim about
  itself, and a log line that can be forged by a header is worse than no line.
  Never a body: it has not been read, and reading one to log it would be the
  expense this exists to avoid.

/health and /ready are exempt. An orchestrator that cannot reach the health
endpoint under load kills the pod, which is the outage this is supposed to
prevent, arriving by a different route. They are also the two cheapest routes
in the service, so exempting them costs nothing worth counting.

THE CAP IS READ LAZILY, per request, never in __init__ (ASSUMPTIONS §10.1). The
body-size middleware that used to live here was removed after exactly that: a
`get_settings()` in a middleware constructor forces config to load at import
time, which broke the tests that build settings themselves. `get_settings()` is
lru_cached, so the per-request read is a dict lookup.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import LoadShed, dodeal_error_response

_logger = logging.getLogger("dodeal_ai.inflight")

# Cheap, always-available, and killed by an outage of their own if they are
# shed. Matched exactly: a path is not a prefix here, so `/healthz` or
# `/health/../judgements` is counted like anything else.
EXEMPT_PATHS = frozenset({"/health", "/ready"})


class InflightCounter:
    """How many requests are inside the app right now.

    A plain integer behind three methods rather than a bare module global,
    because the thing that has to be true of it is that every increment is
    matched by a decrement — and a named object with `release()` on it is
    something a reader can check, whereas `_count -= 1` scattered across a
    dispatch method is something a reader has to trace.

    NO LOCK, deliberately. This counts work on ONE event loop in ONE process:
    the read-compare-increment below never awaits between its steps, so no other
    task can run inside it and there is nothing for a lock to protect. (It is
    per-process for the same reason it is exact — two pods each shed against
    their own ceiling, which is what a per-pod ceiling means.)
    """

    __slots__ = ("_count",)

    def __init__(self) -> None:
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def acquire(self, limit: int) -> bool:
        """Take a slot if one is free. True = admitted, False = shed."""
        if self._count >= limit:
            return False
        self._count += 1
        return True

    def release(self) -> None:
        self._count -= 1


# Module-level and shared by the whole process, which is what "in flight" means.
# It is also how the pipeline reads the number for its outcome line: a unit has
# a TenantScope and no Request, so it cannot reach app.state.
_counter = InflightCounter()


def current_inflight() -> int:
    """The count as of right now, for anything that wants to record it.

    A snapshot and nothing more. It is an observation about the process at the
    moment it was taken, never an input to a decision -- the only code allowed
    to decide on this number is `acquire` above, which reads and acts on it
    without an await in between.
    """
    return _counter.count


class InflightMiddleware(BaseHTTPMiddleware):
    """Admit up to `max_inflight` concurrent requests; refuse the rest with 503.

    The decrement is in a `finally`. A route that raises still leaves the app,
    and a slot leaked on every 500 would eventually shed every request while the
    service sat idle -- an availability bug that only appears after the bug that
    caused it has been happening for a while, which is the worst kind to
    diagnose.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Exposed for anything holding the app rather than the module: the same
        # object, so `app.state.inflight.count` is live and not a snapshot.
        request.app.state.inflight = _counter

        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        if not _counter.acquire(get_settings().max_inflight):
            request_id = getattr(request.state, "request_id", "unknown")
            # The count, not the cap: the cap is in config and a reader can look
            # it up, whereas how many were actually in flight when the refusals
            # started is the number that says whether the cap is wrong.
            _logger.warning(
                "load_shed",
                extra={
                    "reason_code": "load_shed",
                    "request_id": request_id,
                    "inflight": _counter.count,
                },
            )
            return dodeal_error_response(LoadShed(), request_id)

        try:
            return await call_next(request)
        finally:
            _counter.release()
