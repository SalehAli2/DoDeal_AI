"""The two middlewares as PURE ASGI: what the shape buys and what it must not break.

Register item 86. `tests/unit/test_inflight.py` and `tests/security/test_chain.py`
already pin the behaviour -- the cap, the `finally`, the refusal body, the
validated inbound id, the echoed header -- and every one of them passes
unchanged across the rewrite, which is the point. This file pins the two things
that were NOT true before and are now, plus the one case the old base class
never made us think about.

  CONTEXTVARS PROPAGATE, IN THE DIRECTION THAT WAS BROKEN. A BaseHTTPMiddleware
  runs the inner app in a child task of an anyio task group. A child task COPIES
  its parent's context at spawn, so the downward direction (set outside, read in
  the route) worked before this piece and still works -- it proves nothing. The
  upward direction is the one a task boundary severs: a ContextVar set IN THE
  ROUTE lands in the child's copy of the context and is thrown away with it, so
  the middleware that awaited the route sees nothing. That is the property any
  future request-scoped context depends on -- a tenant, a trace span, a cost
  tally set deep in the pipeline and read on the way out.

  A NON-HTTP SCOPE PASSES THROUGH. `lifespan` at startup and shutdown, and
  `websocket` if one is ever added. BaseHTTPMiddleware handled this for us; a
  pure ASGI callable has to do it itself, and getting it wrong is not loud. A
  counted lifespan scope holds its slot until the process exits, so the cap is
  silently one lower for the life of the pod, and an id written into a lifespan
  scope is one id shared by everything that later reads that state.

The inner apps here are raw ASGI callables rather than routes wherever the claim
is about the middleware itself: a FastAPI app between the assertion and the
thing asserted only adds ways for the test to be wrong.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.types import Message, Receive, Scope, Send

from dodeal_ai.core.config import Settings
from dodeal_ai.middleware import inflight
from dodeal_ai.middleware.inflight import (
    InflightCounter,
    InflightMiddleware,
    current_inflight,
)
from dodeal_ai.middleware.request_id import RequestIDMiddleware

_PROBE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "dodeal_test_probe", default="NOT-SET"
)

# `app` is in both because Starlette puts it in EVERY scope it builds, lifespan
# included, before the middleware stack sees it. Leaving it out would make the
# counting assertions below pass for the wrong reason: a middleware that wrongly
# counted a lifespan scope would raise KeyError here rather than count it.
_SCOPE_APP = FastAPI()

_NON_HTTP_SCOPES = [
    pytest.param({"type": "lifespan", "app": _SCOPE_APP}, id="lifespan"),
    pytest.param(
        {"type": "websocket", "path": "/ws", "headers": [], "app": _SCOPE_APP},
        id="websocket",
    ),
]


@pytest.fixture(autouse=True)
def _fresh_counter(monkeypatch: pytest.MonkeyPatch):
    """A counter of its own per test, for the reason test_inflight.py has one:
    the real one is a process-wide global and a test that mutated it would leak
    its state into the next."""
    monkeypatch.setattr(inflight, "_counter", InflightCounter())


@pytest.fixture(autouse=True)
def _cap(monkeypatch: pytest.MonkeyPatch):
    """A cap of two, through the real Settings object, so nothing here reaches a
    developer's environment for the number."""
    monkeypatch.setattr(
        inflight,
        "get_settings",
        lambda: Settings(_env_file=None, jwt_signing_key="test-key", max_inflight=2),
    )


class Recorder:
    """An inner ASGI app that records what it was handed and answers 200."""

    def __init__(self) -> None:
        self.scopes: list[Scope] = []
        self.inflight_seen: list[int] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.scopes.append(scope)
        self.inflight_seen.append(current_inflight())
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})


def _stack(inner: Any) -> Any:
    """The two middlewares in the order main.py installs them: request-id
    OUTSIDE inflight, so a refused request still carries an id."""
    return RequestIDMiddleware(InflightMiddleware(inner))


async def _receive() -> Message:
    return {"type": "http.disconnect"}


def _sender(messages: list[Message]) -> Send:
    async def _send(message: Message) -> None:
        messages.append(message)

    return _send


# --- non-http scopes pass through -------------------------------------------


@pytest.mark.parametrize("scope", _NON_HTTP_SCOPES)
async def test_a_non_http_scope_reaches_the_inner_app_untouched(scope: Scope):
    """The same scope object, with nothing added to it -- `state` included."""
    inner = Recorder()
    before = dict(scope)

    await _stack(inner)(scope, _receive, _sender([]))

    assert len(inner.scopes) == 1
    assert inner.scopes[0] is scope
    assert scope == before
    assert "state" not in scope


@pytest.mark.parametrize("scope", _NON_HTTP_SCOPES)
async def test_a_non_http_scope_takes_no_slot(scope: Scope):
    """Counted, a lifespan scope would hold its slot until the process exits and
    the cap would be one lower for the life of the pod."""
    inner = Recorder()

    await _stack(inner)(scope, _receive, _sender([]))

    assert inner.inflight_seen == [0]
    assert current_inflight() == 0


async def test_an_http_scope_on_the_same_stack_is_counted_and_identified():
    """The control for the two above: same middlewares, same inner app, an
    `http` scope -- and now the slot IS taken and the id IS written. A
    pass-through that let http requests through too would pass both of them."""
    inner = Recorder()
    app = FastAPI()
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/anything",
        "headers": [],
        "app": app,
    }
    messages: list[Message] = []

    await _stack(inner)(scope, _receive, _sender(messages))

    assert inner.inflight_seen == [1]
    assert current_inflight() == 0
    request_id = scope["state"]["request_id"]
    assert request_id
    assert scope["state"]["observability"].request_id == request_id
    assert app.state.inflight is inflight._counter
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert (b"x-request-id", request_id.encode()) in start["headers"]


# --- contextvars, in the direction a task boundary breaks --------------------


class ProbeReader:
    """An outermost middleware that reads the ContextVar AFTER the inner app has
    returned, and records the task it ran in."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.seen: str | None = None
        self.task: asyncio.Task[Any] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.task = asyncio.current_task()
        await self.app(scope, receive, send)
        self.seen = _PROBE.get()


def _probe_app(holder: list[ProbeReader]) -> FastAPI:
    """main.py's stack with one more middleware outside it, holding the probe."""
    app = FastAPI()
    app.add_middleware(InflightMiddleware)
    app.add_middleware(RequestIDMiddleware)

    def _hold(inner: Any) -> ProbeReader:
        reader = ProbeReader(inner)
        holder.append(reader)
        return reader

    app.add_middleware(_hold)

    @app.get("/probe")
    async def probe(request: Request) -> dict[str, Any]:
        _PROBE.set("SET-IN-ROUTE")
        return {
            "request_id": getattr(request.state, "request_id", "unknown"),
            "task": id(asyncio.current_task()),
        }

    return app


async def _get_probe(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get("/probe")


async def test_a_contextvar_set_in_the_route_is_visible_to_an_outer_middleware():
    """THE REASON FOR THE PIECE, in the one direction that discriminates.

    Downward propagation worked before the rewrite too, because a child task
    copies its parent's context at spawn; asserting it would prove nothing.
    Upward does not survive a task boundary at all. Run against the
    BaseHTTPMiddleware versions this assertion reads "NOT-SET".
    """
    holder: list[ProbeReader] = []
    response = await _get_probe(_probe_app(holder))

    assert response.status_code == 200
    assert holder[0].seen == "SET-IN-ROUTE"


async def test_the_route_runs_in_the_same_task_as_the_middleware():
    """The mechanism behind the test above, asserted directly: no task group, no
    child task, one task from the outermost middleware to the endpoint."""
    holder: list[ProbeReader] = []
    response = await _get_probe(_probe_app(holder))

    assert response.status_code == 200
    assert response.json()["task"] == id(holder[0].task)


async def test_the_request_id_still_reaches_the_route_through_request_state():
    """And the contract every consumer actually uses -- `request.state` as a view
    over `scope["state"]` -- gives the route a real id, not "unknown"."""
    response = await _get_probe(_probe_app([]))

    body = response.json()
    assert body["request_id"] != "unknown"
    assert body["request_id"] == response.headers["X-Request-ID"]


# --- the send wrapper does not mutate what it was handed ---------------------


async def test_the_id_header_is_added_to_a_copy_not_to_the_inner_apps_list():
    """A Response sends its own `raw_headers` list, and a Response can be sent
    more than once. Appending in place would add one x-request-id per send, so
    the second response from a reused object would carry two."""
    original_headers: list[tuple[bytes, bytes]] = [(b"content-type", b"text/plain")]

    class SharedHeaders:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": original_headers,
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

    messages: list[Message] = []
    scope: Scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}

    await RequestIDMiddleware(SharedHeaders())(scope, _receive, _sender(messages))

    assert original_headers == [(b"content-type", b"text/plain")]
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert len(start["headers"]) == 2
