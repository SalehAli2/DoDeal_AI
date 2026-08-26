"""Request-id middleware -- the PRODUCER for request.state.request_id.

Every existing consumer (core/audit/logger.py, core/errors.py, RequestContext
via core/context.py) already reads request.state.request_id via
getattr(request.state, "request_id", "unknown") -- but nothing ever SET it,
so it was always "unknown". This is that producer.

Ordering: registered as ordinary Starlette middleware via app.add_middleware.
Starlette always wraps the whole app -- including any user middleware -- in
its own built-in ServerErrorMiddleware, unconditionally outermost regardless
of registration order. So this runs inside that fail-closed boundary but
before routing/the gate chain: even a request that fails before reaching a
route still has a real request_id available to the error handler and any
audit line already emitted.

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

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_REQUEST_ID_HEADER = "X-Request-ID"

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


def _accepted_inbound_id(request: Request) -> str | None:
    """The inbound X-Request-ID if it is well-formed, else None (treated
    exactly as an absent header). A rejected value is silently dropped and
    never logged, echoed, or reported to the caller."""
    inbound = request.headers.get(_REQUEST_ID_HEADER)
    if inbound is not None and _VALID_REQUEST_ID.fullmatch(inbound):
        return inbound
    return None


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Reads an inbound X-Request-ID if it is present AND well-formed,
    otherwise generates one (uuid4). Sets request.state.request_id (existing
    contract) and request.state.observability (fuller shape). Echoes the id
    back as an X-Request-ID response header so a caller can correlate."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = _accepted_inbound_id(request) or str(uuid.uuid4())
        request.state.request_id = request_id
        request.state.observability = RequestObservability(
            request_id=request_id, trace_id=request_id
        )

        response = await call_next(request)
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response
