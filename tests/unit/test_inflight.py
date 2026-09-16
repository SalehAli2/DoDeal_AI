"""Load shedding: the cap admits, the cap refuses, and the slot always comes back.

The three claims worth testing are not "a 503 came out" — an unconditional 503
would pass that. They are:

  THE COUNT IS EXACT. Not "some requests were refused" but "exactly `cap` were
  admitted and exactly the rest were shed", proved by holding `cap + n` requests
  open at once inside a slow route and counting both outcomes. A counter that
  drifts under concurrency sheds requests the service could have served, which
  is the failure this middleware would be causing rather than preventing.

  THE SLOT COMES BACK, on every exit. The success path is easy; the one that
  matters is a route that RAISES, because a slot leaked per 500 sheds more and
  more traffic while the service sits idle, and the symptom appears long after
  the bug that caused it.

  THE REFUSAL IS CHEAP AND QUIET. No tenant on the line (the gates have not
  run), no body on it (it was never read), and the id echoed so the caller can
  quote it.

The routes here are STUBS registered on a throwaway app for the duration of one
test. The real routes need a token, a host, a fake Redis and a fake model to
reach at all, and none of that is what is being tested: this middleware runs
before every one of them and knows about none of them.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dodeal_ai.core import inflight as core_inflight
from dodeal_ai.core.config import (
    ConfigError,
    Settings,
    _build_settings,
    get_settings,
)
from dodeal_ai.core.errors import register_error_handlers
from dodeal_ai.core.inflight import InflightCounter, current_inflight
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.middleware import inflight
from dodeal_ai.middleware.inflight import EXEMPT_PATHS, InflightMiddleware
from dodeal_ai.middleware.request_id import RequestIDMiddleware

# Shaped like a note body, because the point of the assertion is that a REFUSED
# request's body never reaches a log line — and the body of a refused request is
# the one thing this middleware is guaranteed never to have read.
SENTINEL = "SENTINEL-0501234567 villa budget 4.2M"


@pytest.fixture(autouse=True)
def _fresh_counter(monkeypatch):
    """A counter of its own per test.

    The real one is a module global, which is what "in flight in this process"
    means; a test that mutated it would leak its state into the next. Replacing
    it is safe because nothing captures it at import time — `current_inflight`
    and the middleware both read the module attribute when they run.
    """
    monkeypatch.setattr(core_inflight, "_counter", InflightCounter())


def _settings(max_inflight: int) -> Settings:
    return Settings(
        _env_file=None, jwt_signing_key="test-key", max_inflight=max_inflight
    )


@pytest.fixture
def cap(monkeypatch):
    """Set the cap for one test, through the real settings object."""

    def _set(value: int):
        monkeypatch.setattr(inflight, "get_settings", lambda: _settings(value))

    return _set


def _app(*, gate: asyncio.Event | None = None) -> FastAPI:
    """A throwaway app with the two middlewares in the order main.py installs
    them, the real error handlers, and three stub routes.

    Registered inflight-first so request-id ends up OUTSIDE it — the same
    reversal main.py documents. If that order were wrong here the refusal would
    still work and would quietly carry `request_id: "unknown"`, so
    `test_the_refusal_carries_the_request_id` is what pins it.
    """
    app = FastAPI()
    app.add_middleware(InflightMiddleware)
    app.add_middleware(RequestIDMiddleware)
    register_error_handlers(app)

    @app.get("/fast")
    async def fast():
        return {"ok": True}

    @app.post("/slow")
    async def slow():
        # Held open by the test, so every caller is inside the app at once.
        assert gate is not None
        await gate.wait()
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        return JSONResponse({"status": "ready"})

    return app


@pytest.fixture
def log_capture():
    """The real JsonFormatter over the whole `dodeal_ai` tree at DEBUG: the exact
    text a collector would receive, from every logger in the package."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


# --- below the cap ----------------------------------------------------------


def test_a_request_below_the_cap_passes(cap):
    cap(2)
    with TestClient(_app()) as client:
        assert client.get("/fast").status_code == 200


def test_the_counter_returns_to_zero(cap):
    cap(2)
    with TestClient(_app()) as client:
        for _ in range(5):
            assert client.get("/fast").status_code == 200
    # Five sequential requests, five slots taken and five given back.
    assert current_inflight() == 0


def test_a_request_that_raises_still_decrements(cap):
    # The finally, on its own. Without it a slot leaks per 500 and the service
    # sheds more and more traffic while doing nothing.
    cap(2)
    with TestClient(_app(), raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500
        assert client.get("/boom").status_code == 500

    assert current_inflight() == 0
    # And the cap is still usable afterwards, which is what a leak would break.
    with TestClient(_app(), raise_server_exceptions=False) as client:
        assert client.get("/fast").status_code == 200


# --- at the cap -------------------------------------------------------------


async def _hold(app: FastAPI, gate: asyncio.Event, requests: int) -> list[int]:
    """Fire `requests` concurrent calls at the slow route and return the status
    codes, in argument order.

    An ASGI transport rather than TestClient: TestClient runs each request in a
    worker thread with its own loop, so requests would not be inside the app at
    the same time and the cap would never be met. Here every call shares this
    test's loop, exactly as they would share the server's.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:

        async def _one():
            return await client.post("/slow", json={"note_text": SENTINEL})

        tasks = [asyncio.create_task(_one()) for _ in range(requests)]
        # Let every task reach the route (or be refused) before releasing.
        for _ in range(200):
            await asyncio.sleep(0)
        gate.set()
        return [r.status_code for r in await asyncio.gather(*tasks)]


async def test_the_request_at_the_cap_is_refused(cap, log_capture):
    cap(1)
    gate = asyncio.Event()
    statuses = await _hold(_app(gate=gate), gate, 2)

    assert statuses == [200, 503]
    assert current_inflight() == 0


async def test_the_refusal_is_the_load_shed_body(cap):
    cap(1)
    gate = asyncio.Event()
    app = _app(gate=gate)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        held = asyncio.create_task(client.post("/slow", json={}))
        for _ in range(200):
            await asyncio.sleep(0)

        refused = await client.get("/fast")
        gate.set()
        await held

    assert refused.status_code == 503
    body = refused.json()
    assert body["detail"] == "Service Unavailable"
    assert body["reason"] == "load_shed"
    assert body["request_id"]


async def test_the_refusal_carries_the_request_id(cap):
    # The id is the whole reason this middleware sits INSIDE the request-id one.
    # If the order were reversed the refusal would still 503 and would silently
    # carry "unknown" instead.
    cap(1)
    gate = asyncio.Event()
    app = _app(gate=gate)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        held = asyncio.create_task(client.post("/slow", json={}))
        for _ in range(200):
            await asyncio.sleep(0)

        refused = await client.get("/fast", headers={"X-Request-ID": "req-shed-1"})
        gate.set()
        await held

    assert refused.json()["request_id"] == "req-shed-1"
    assert refused.headers["X-Request-ID"] == "req-shed-1"


async def test_the_refusal_is_logged_once_with_the_id_and_the_count(cap, log_capture):
    cap(1)
    gate = asyncio.Event()
    await _hold(_app(gate=gate), gate, 2)

    shed = [x for x in _lines(log_capture) if x["message"] == "load_shed"]
    assert len(shed) == 1
    assert shed[0]["reason_code"] == "load_shed"
    assert shed[0]["inflight"] == 1  # what was in flight when it was refused
    assert shed[0]["request_id"]


async def test_the_refusal_line_carries_no_tenant(cap, log_capture):
    # The gates have not run. The only tenant available would be the caller's
    # own unverified claim about itself, and a log field that can be set by a
    # header is worse than no field.
    cap(1)
    gate = asyncio.Event()
    await _hold(_app(gate=gate), gate, 2)

    shed = next(x for x in _lines(log_capture) if x["message"] == "load_shed")
    assert "tenant" not in shed


async def test_a_refused_requests_body_reaches_no_log_line(cap, log_capture):
    # `_hold` posts the sentinel as the body of every request, so the refused
    # one carried it. The middleware never reads a body — this is the test that
    # keeps it that way.
    cap(1)
    gate = asyncio.Event()
    statuses = await _hold(_app(gate=gate), gate, 2)

    assert 503 in statuses
    text = log_capture.getvalue()
    assert "SENTINEL" not in text
    assert "0501234567" not in text


# --- the exact count under real concurrency ---------------------------------


async def test_the_count_is_exact_under_twenty_concurrent_requests(cap):
    """Twenty at once against a cap of five: exactly five in, exactly fifteen out.

    The claim is EXACTNESS, not "shedding happened". A counter that drifted --
    incremented after an await, decremented twice on one path, or read outside
    the compare -- would still refuse some requests and still look like it was
    working, while quietly refusing requests the service had room for.
    """
    cap(5)
    gate = asyncio.Event()
    statuses = await _hold(_app(gate=gate), gate, 20)

    assert statuses.count(200) == 5
    assert statuses.count(503) == 15
    assert len(statuses) == 20
    assert current_inflight() == 0


async def test_every_admitted_request_is_one_of_the_first_arrivals(cap):
    # Argument order is arrival order here: the tasks are created in order and
    # each one's first step runs in that order. So the admitted five must be the
    # FIRST five, and a counter that admitted a later one would mean the compare
    # and the increment had an await between them.
    cap(5)
    gate = asyncio.Event()
    statuses = await _hold(_app(gate=gate), gate, 20)

    assert statuses[:5] == [200] * 5
    assert statuses[5:] == [503] * 15


async def test_the_cap_is_reusable_after_a_burst(cap):
    # Every slot taken by the burst was given back, so the next request is
    # served rather than shed.
    cap(2)
    gate = asyncio.Event()
    await _hold(_app(gate=gate), gate, 6)

    with TestClient(_app()) as client:
        assert client.get("/fast").status_code == 200


# --- the exempt paths -------------------------------------------------------


@pytest.mark.parametrize("path", sorted(EXEMPT_PATHS))
async def test_health_and_ready_bypass_the_cap(cap, path):
    """An orchestrator that cannot reach /health under load kills the pod — the
    outage this middleware exists to prevent, arriving by another route."""
    cap(1)
    gate = asyncio.Event()
    app = _app(gate=gate)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        held = asyncio.create_task(client.post("/slow", json={}))
        for _ in range(200):
            await asyncio.sleep(0)

        # The cap is full: /fast would be refused right now.
        assert (await client.get("/fast")).status_code == 503
        assert (await client.get(path)).status_code == 200

        gate.set()
        await held


async def test_an_exempt_path_takes_no_slot(cap):
    # Exempt means not counted, not merely not refused: a /health that consumed
    # a slot would shed a real request under a tight cap.
    cap(1)
    gate = asyncio.Event()
    app = _app(gate=gate)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        held = asyncio.create_task(client.get("/health"))
        for _ in range(50):
            await asyncio.sleep(0)
        await held
        assert current_inflight() == 0

        gate.set()


# --- the counter itself -----------------------------------------------------


def test_the_counter_admits_up_to_the_limit_and_no_further():
    counter = InflightCounter()
    assert counter.acquire(2) is True
    assert counter.acquire(2) is True
    assert counter.acquire(2) is False
    assert counter.count == 2  # a refusal takes nothing


def test_releasing_frees_a_slot():
    counter = InflightCounter()
    counter.acquire(1)
    assert counter.acquire(1) is False
    counter.release()
    assert counter.acquire(1) is True


def test_the_count_is_exposed_on_app_state(cap):
    # Commit L.3 reads the number for the outcome line, and anything holding the
    # app can read it here. The OBJECT is stored, not a snapshot, so `.count` is
    # live rather than whatever it was during some earlier request.
    cap(2)
    app = _app()
    with TestClient(app) as client:
        client.get("/fast")

    assert app.state.inflight is core_inflight._counter
    assert app.state.inflight.count == 0


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_cap_is_refused_at_startup(value):
    # 0 would refuse every request including the first: a config typo that looks
    # exactly like an outage, and one that would be diagnosed as one. It fails
    # closed at settings load instead.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_signing_key="k", max_inflight=value)


def test_a_non_positive_cap_refuses_the_whole_service(monkeypatch):
    # And through the real accessor it is a ConfigError, which is what startup
    # and /ready treat as "refuse to run", exactly like a missing signing key.
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_MAX_INFLIGHT", "0")
    with pytest.raises(ConfigError):
        _build_settings(_env_file=None)


def test_the_default_cap_is_the_provisional_thirty_two(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()
    try:
        assert Settings(_env_file=None, jwt_signing_key="k").max_inflight == 32
    finally:
        get_settings.cache_clear()
