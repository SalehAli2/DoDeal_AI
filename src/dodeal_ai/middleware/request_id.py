"""Request-id middleware -- the PRODUCER for request.state.request_id.

Every existing consumer (core/audit/logger.py, core/errors.py, RequestContext
via core/context.py) already reads request.state.request_id via
getattr(request.state, "request_id", "unknown") -- but nothing ever SET it,
so it was always "unknown". This is that producer.

SHAPE: a plain ASGI callable -- __init__(app) and __call__(scope, receive,
send) -- registered with app.add_middleware exactly as before (register item
86). It was a BaseHTTPMiddleware, which runs EVERY request through an anyio
task group and a pair of memory object streams so that `dispatch` can be
written against a Request and a Response. Nothing here wants either: the id is
three dict writes and one header. What the wrapper cost was a task group per
request, a route running in a CHILD task (so a contextvars.ContextVar set in
the route is invisible to anything outside this middleware), and cancellation
semantics that have moved between Starlette releases -- 1.3.1 is pinned, and a
pin is a deferral, not a fix.

scope["state"] is not a second place to keep the id. Request.state is a VIEW
over exactly that dict -- Starlette's HTTPConnection.state does
`scope.setdefault("state", {})` and then wraps it -- so setting the key here IS
setting request.state.request_id, and every getattr(..., "unknown") consumer
keeps reading it with no change of its own.

Ordering: Starlette always wraps the whole app -- including any user
middleware -- in its own built-in ServerErrorMiddleware, unconditionally
outermost regardless of registration order. So this runs inside that
fail-closed boundary but before routing/the gate chain: even a request that
fails before reaching a route still has a real request_id available to the
error handler and any audit line already emitted.

A non-http scope (`lifespan` at startup and shutdown, `websocket` if one is
ever added) passes straight through untouched. There is no request to identify,
a lifespan scope has no headers to read, and its scope outlives every request
in the process -- writing an id into it would hand one id to all of them.

The inbound header is caller-controlled, so it is VALIDATED, not trusted: the
id reaches every audit line and the response header, so an unchecked value
could inject newlines into the log stream (forging a second record) or a "{"
that a downstream collector tries to parse. Anything that does not match
_VALID_REQUEST_ID is discarded and a uuid4 is generated as if no header had
been sent. The rejected value is never logged -- logging it would put the
untrusted bytes into the very stream the check protects.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ASGI carries header names as lower-cased bytes, in the request scope and in
# the response-start message alike. This is the same header as the `X-Request-ID`
# spelling a caller sends: HTTP names are case-insensitive, and Starlette
# lower-cased it on the way out too, so the bytes on the wire are unchanged.
_REQUEST_ID_HEADER = b"x-request-id"

# Accepted inbound ids: the character set every common correlation-id format
# uses (uuid, ULID, W3C trace id, hyphen/underscore/dot-joined tokens), capped
# at 128 so a caller cannot pad a log line. Matched with fullmatch() and
# anchored \A...\Z rather than ^...$ -- deliberately belt-and-braces, because
# $ alone also matches BEFORE a trailing newline and would admit the exact
# injection this rejects.
_VALID_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")


@dataclass(frozen=True)
class RequestObservability:
    """The fuller per-request id/version set (FUTURE_PATTERNS.md item 7).
    request_id/trace_id are real today; prompt_version/model_version/
    workflow_version stay unset until a real LLM call exists to supply
    them, so adding them later is a field, not a rebuild.

    request_id is ALSO set directly on request.state.request_id (unchanged
    contract) for every existing getattr(..., "unknown") consumer; this
    object is the fuller structure future features read from.
    """

    request_id: str
    trace_id: str
    prompt_version: str | None = None
    model_version: str | None = None
    workflow_version: str | None = None


def _inbound_header(scope: Scope) -> str | None:
    """The raw inbound X-Request-ID, or None if the caller sent none.

    FIRST occurrence wins, which is what Headers.get did before: a caller who
    sends the header twice does not get to choose which copy is checked.
    latin-1 is the decoding HTTP header bytes have and the one Starlette uses --
    it cannot raise, so a caller cannot reach this code with an exception.
    """
    for name, value in scope["headers"]:
        if name == _REQUEST_ID_HEADER:
            return str(value.decode("latin-1"))
    return None


def _accepted_inbound_id(scope: Scope) -> str | None:
    """The inbound X-Request-ID if it is well-formed, else None (treated
    exactly as an absent header). A rejected value is silently dropped and
    never logged, echoed, or reported to the caller."""
    inbound = _inbound_header(scope)
    if inbound is not None and _VALID_REQUEST_ID.fullmatch(inbound):
        return inbound
    return None


class RequestIDMiddleware:
    """Reads an inbound X-Request-ID if it is present AND well-formed,
    otherwise generates one (uuid4). Sets request.state.request_id (existing
    contract) and request.state.observability (fuller shape). Echoes the id
    back as an X-Request-ID response header so a caller can correlate."""

    __slots__ = ("app",)

    def __init__(self, app: ASGIApp) -> None:
        # The inner app and nothing else. No settings read, no regex compiled,
        # no id generated: a constructor runs at import time in some test
        # layouts, and anything done here is done once for the process rather
        # than once per request (ASSUMPTIONS 10.1).
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _accepted_inbound_id(scope) or str(uuid.uuid4())
        # setdefault, not assignment: an ASGI server may already have put a
        # `state` dict in the scope (uvicorn hands each request a shallow copy
        # of the lifespan state), and replacing it would drop whatever the
        # lifespan put there. This dict is what Request.state reads.
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["observability"] = RequestObservability(
            request_id=request_id, trace_id=request_id
        )

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                # A NEW list and a new message, never an append in place. The
                # message is the inner app's object and its `headers` may be a
                # Response's own `raw_headers`, which that Response can send
                # again; appending in place would grow it one id per send.
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (_REQUEST_ID_HEADER, request_id.encode("latin-1")),
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_with_request_id)
