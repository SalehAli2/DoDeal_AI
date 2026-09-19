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
lru_cached, so the per-request read is a dict lookup. __init__ now genuinely
exists and takes the inner app and nothing else, which is the whole of it.

  THE SHAPE IS A PLAIN ASGI CALLABLE, `__init__(app)` plus `__call__(scope,
  receive, send)`, registered with add_middleware exactly as before (register
  item 86). It was a BaseHTTPMiddleware, and that is the one base class this
  middleware could least afford: it runs every request through an anyio task
  group and a pair of memory object streams so `dispatch` can be handed a
  Request and return a Response. The refusal here reads one integer. Paying for
  a task group to decide not to do any work is the defence costing more than
  the work it refuses, which is the thing the paragraph above says it must
  never be. (It also ran the route in a child task, so a contextvars.ContextVar
  set in a route reached nothing outside this middleware.)
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from dodeal_ai.core import inflight as core_inflight
from dodeal_ai.core import metrics
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import LoadShed, dodeal_error_response

_logger = logging.getLogger("dodeal_ai.inflight")

# Cheap, always-available, and killed by an outage of their own if they are
# shed. Matched exactly: a path is not a prefix here, so `/healthz` or
# `/health/../judgements` is counted like anything else.
EXEMPT_PATHS = frozenset({"/health", "/ready", "/metrics"})


class InflightMiddleware:
    """Admit up to `max_inflight` concurrent requests; refuse the rest with 503.

    The decrement is in a `finally`. A route that raises still leaves the app,
    and a slot leaked on every 500 would eventually shed every request while the
    service sat idle -- an availability bug that only appears after the bug that
    caused it has been happening for a while, which is the worst kind to
    diagnose.
    """

    __slots__ = ("app",)

    def __init__(self, app: ASGIApp) -> None:
        # The inner app and nothing else -- see THE CAP IS READ LAZILY above.
        # The counter is NOT captured here either: core/inflight.py's `_counter`
        # is looked up per request, which is what lets a test swap the
        # process-wide counter for one of its own without rebuilding the app.
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # A `lifespan` or `websocket` scope is not a request. Neither can be
        # shed -- there is no 503 to send a lifespan -- and counting one would
        # be worse than pointless: a lifespan scope lasts as long as the process,
        # so its slot would never come back and the cap would be one lower for
        # the life of the pod.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Read once, so the slot taken and the slot released are on one counter.
        counter = core_inflight._counter

        # Exposed for anything holding the app rather than the module: the same
        # object, so `app.state.inflight.count` is live and not a snapshot.
        scope["app"].state.inflight = counter

        if scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        if not counter.acquire(get_settings().max_inflight):
            # Read from the scope dict that RequestIDMiddleware -- outside this
            # one -- wrote, which is the same dict `request.state` is a view
            # over. "unknown" if this is somehow reached without it, exactly as
            # the getattr default did.
            request_id = scope.get("state", {}).get("request_id", "unknown")
            metrics.LOAD_SHED.inc()
            # The count, not the cap: the cap is in config and a reader can look
            # it up, whereas how many were actually in flight when the refusals
            # started is the number that says whether the cap is wrong.
            _logger.warning(
                "load_shed",
                extra={
                    "reason_code": "load_shed",
                    "request_id": request_id,
                    "inflight": counter.count,
                },
            )
            # A Response IS an ASGI app: calling it sends the same status, the
            # same headers and the same body bytes that returning it from a
            # dispatch produced, through the one builder every enumerated error
            # shares (core/errors.py explains why a middleware cannot raise).
            await dodeal_error_response(LoadShed(), request_id)(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            # Register item 73, and the reason this is a `finally` and not a
            # line after the await: an exception from the inner app now
            # propagates straight up to ServerErrorMiddleware, so a `release()`
            # on the success path alone would leak a slot on every 500.
            counter.release()
