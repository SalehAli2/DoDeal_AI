"""Body-size limit — refuse the bytes before anything reads them.

Register item 87. Nothing in the app bounded a request body: a caller could
send megabytes and the app would read every one of them into memory before
Gate 1 decided whether that caller was allowed to send anything at all. The
cost of a request has to be bounded BEFORE the request is identified, or the
identification is the thing paying for it.

  WHERE IT SITS. OUTERMOST, the last `add_middleware` call in main.py, so a
  refusal happens before the request id, before load shedding, before routing
  and before all four gates. That ordering is the whole design: the refusal
  costs a header read, and anything placed above it would be work done on
  behalf of a request we were always going to refuse.

  The price of being outermost is that the request id does not exist yet on
  the header path. `dodeal_error_response` is handed "unknown" there, and that
  is correct rather than unfortunate -- the register places the byte check
  before everything, the id middleware included, and generating an id here to
  make the body look tidier would mean two middlewares minting ids. The
  streamed path below does carry a real id, for a reason that is a consequence
  of where it refuses and not of a second policy.

  WHAT IT LOGS. Nothing. The 413 is the record. A WARNING per oversized
  request would be a log-volume lever a caller controls: anyone who can send a
  large body can write a line to our logs, and the cheaper it is for them the
  more lines they get -- which is the same asymmetry this middleware exists to
  close, arriving through the log pipeline instead of through memory.

TWO PATHS, because there are two ways a body arrives.

  THE HEADER PATH is the one that matters. A `content-length` above the cap is
  refused without reading a byte and without ever calling `receive`. That is
  the cheap refusal, and it is what an honest client (and every HTTP library
  sending a buffered body) gets.

  THE STREAMED PATH is for `Transfer-Encoding: chunked`, for a client that
  sends no length, and for a client that LIES about its length -- the header
  is the caller's claim about itself, so it can bound the cheap path but can
  never be the only check. Every `http.request` message's body is counted as
  it goes past; the count is an integer, never a copy of the bytes, so nothing
  here buffers what it is refusing.

  A body of exactly the cap is admitted; one byte more is refused (`>`, not
  `>=`). The cap is the largest body that is allowed, which is what a number
  named "max" means.

WHY THE MIDDLEWARE SENDS THE 413 ITSELF, on both paths. The obvious design for
the streamed path is to raise `PayloadTooLarge` and let the `DodealError`
handler render it. It does not work: FastAPI catches EVERY exception out of
`await request.body()` and re-raises it as its own `HTTPException(400, "There
was an error parsing the body")` (fastapi/routing.py, and it is deliberate
there), so the caller would be told 400 -- "your request is malformed" -- for a
request that is merely too big. Making the exception an `HTTPException` as well
so FastAPI's `except HTTPException: raise` branch re-raises it is worse: the
combined MRO redirects `DodealError.__init__`'s `super().__init__(reason_code)`
into `HTTPException.__init__`, which reads the reason code as a status.

So the refusal is not delegated. The wrapped `receive` raises to stop the read
where it stands, and the wrapped `send` DISCARDS whatever the inner app
answered with, and this middleware sends the 413. What the framework inside
decided to make of the exception stops mattering, which is the property worth
having across a FastAPI upgrade.

  The one case it cannot take back is a response that has already STARTED --
  a route that streams a reply while still reading the request. Retracting
  those bytes is not something ASGI offers, so `_started` passes the rest
  through untouched rather than corrupting the response. No route in this
  service does that today.

/health and /ready are NOT exempt, unlike in inflight.py. They carry no body,
so the check costs them a dict lookup and nothing else, and an exemption would
be a path a caller can aim a large body at -- the exact bytes this refuses,
through the two routes that skip the cap.

THE CAP IS READ LAZILY, per request, never in __init__ (ASSUMPTIONS 10.1).
This is the middleware that section is about: the first build of it called
`get_settings()` in its constructor, which forced config to load at import
time and broke the tests that build their own settings. `get_settings()` is
lru_cached, so the per-request read is a dict lookup. `__init__` takes the
inner app and nothing else.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import PayloadTooLarge, dodeal_error_response

# ASGI carries header names as lower-cased bytes. Read rather than trusted: it
# bounds the cheap path only, and the streamed count below is what actually
# holds when the header is absent, chunked, or a lie.
_CONTENT_LENGTH_HEADER = b"content-length"


def _declared_length(scope: Scope) -> int | None:
    """The caller's own claim about its body size, or None if it made none.

    FIRST occurrence wins, as request_id.py does with its header: a caller who
    sends `content-length` twice does not get to choose which copy is checked.
    Anything that is not a plain non-negative integer -- a negative number, a
    duplicate-folded "12, 34", any other junk -- returns None and falls to the
    streamed path, which counts real bytes and cannot be talked out of it.
    """
    for name, value in scope["headers"]:
        if name == _CONTENT_LENGTH_HEADER:
            try:
                declared = int(value)
            except ValueError:
                return None
            return declared if declared >= 0 else None
    return None


def _request_id(scope: Scope) -> str:
    """The id RequestIDMiddleware wrote, or "unknown" if it has not run yet.

    Outermost, on the header path, it has not: "unknown" is the honest answer
    and the module docstring says why. On the streamed path it HAS -- the
    overflow is noticed while the route is reading the body, which is inside
    every middleware below this one -- so that 413 carries a real id. Same
    lookup either way; the difference is when it is reached, not what it does.
    """
    return scope.get("state", {}).get("request_id", "unknown")


class BodyLimitMiddleware:
    """Refuse a request body over `max_request_body_bytes` with 413.

    Holds the inner app and nothing else. Every piece of per-request state --
    the cap, the running count, whether the response has started -- lives in
    `__call__`, because an attribute on `self` would be shared by every request
    in the process and this middleware is one object for the life of the app.
    """

    __slots__ = ("app",)

    def __init__(self, app: ASGIApp) -> None:
        # The inner app and nothing else -- see THE CAP IS READ LAZILY above.
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # A `lifespan` or `websocket` scope has no request body to bound, no
        # `content-length` to read and no 413 to be sent. It passes through
        # untouched, exactly as it does in the other two middlewares.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cap = get_settings().max_request_body_bytes

        declared = _declared_length(scope)
        if declared is not None and declared > cap:
            # The cheap refusal: `receive` is never called, so not one byte of
            # the body is read off the socket. A JSONResponse IS an ASGI app,
            # and this is the same builder every enumerated error shares.
            await dodeal_error_response(PayloadTooLarge(), _request_id(scope))(
                scope, receive, send
            )
            return

        received = 0
        refused = False
        started = False

        async def counting_receive() -> Message:
            """Count what goes past; raise the moment the total passes the cap.

            The raise stops the inner app's read where it stands rather than
            letting it finish assembling a body we have already decided to
            refuse. Whatever the framework makes of the exception is handled by
            the send wrapper below, so it does not matter what that is.
            """
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    refused = True
                    raise PayloadTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            """Drop the inner app's answer once the body has been refused.

            FastAPI turns the raise above into a 400 "error parsing the body".
            That answer is wrong and it is discarded here -- but only while the
            response has not STARTED, because the first byte of a response
            cannot be taken back once it is on the wire.
            """
            nonlocal started
            if refused and not started:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except PayloadTooLarge:
            # The other way it arrives: an inner app that does NOT swallow the
            # exception -- a plain ASGI app, or a route with no body field.
            # Both routes converge on the one refusal below.
            pass

        if refused and not started:
            # `send`, not `guarded_send`: this IS the answer, and the wrapper
            # is holding the door shut against everything else.
            await dodeal_error_response(PayloadTooLarge(), _request_id(scope))(
                scope, receive, send
            )
