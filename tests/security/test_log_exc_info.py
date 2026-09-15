"""Sentinel tests: no record from ANY logger in the process can put an
exception's message on stdout -- only its type and its frames.

The hole this file guards is outside the `dodeal_ai` tree. Inside it, no log
call passes `exc_info` and `core/errors.py`'s catch-all logs frames by hand.
But starlette's `ServerErrorMiddleware`, uvicorn's error logger and any library
in the process log an exception with `exc_info=True` through OUR root handler,
and `JsonFormatter` used to render that with `self.formatException` -- which
prints `str(exc)` and every message in the `__cause__`/`__context__` chain. A
foreign exception's message is frequently the data that failed: a note body, a
model completion, a backend error string (ASSUMPTIONS 3.3, register item 88).

Every test below drives a real chained failure whose two messages are sentinels
and asserts they reach no captured output, while `exc_type` and `exc_frames` --
which carry no content -- do. `capfd` and not `caplog`: the claim is about the
BYTES ON STDOUT under the real `configure_logging()`, so the capture has to be
at the file descriptor, where a uvicorn-style logger's line is seen too.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.logging_config import (
    _UVICORN_LOGGERS,
    JsonFormatter,
    configure_logging,
)
from dodeal_ai.main import app

# Shaped like the content this service handles, and passed as ARGUMENTS
# everywhere below: a traceback quotes the source text of every frame it lists,
# so a sentinel written down in a raising frame would be printed by the frames
# themselves and prove nothing about the message.
SENTINEL_OUTER = "SENTINEL-OUTER-0501234567 villa budget 4.2M"
SENTINEL_INNER = "SENTINEL-INNER-0507654321 second note body"

# The test-only route. Registered on the real app by the fixture below and
# removed again; never added to src/, where a route that raises on purpose has
# no business being.
BOOM = "/_test_sentinel_boom"

# What a library logs from inside a request: the event name as the message and
# the exception through exc_info, which is the shape uvicorn uses for
# "Exception in ASGI application".
THIRD_PARTY = "third_party_lib"


def _raise_chained(outer: str, inner: str) -> None:
    """Raise a foreign exception carrying `outer`, chained from one carrying
    `inner`. Both messages arrive as arguments, so neither is in a source line
    a traceback would quote."""
    try:
        raise ValueError(inner)
    except ValueError as cause:
        raise RuntimeError(outer) from cause


async def sentinel_boom() -> None:
    """A route that fails the way a dependency does: something raises, a
    library logs it with exc_info, and the exception goes on to the catch-all.

    The log call is here rather than in a wrapper because TestClient never runs
    uvicorn, and uvicorn is what logs an escaping exception with exc_info in
    production. A library logging inside the request reaches the SAME root
    handler by the same path, which is the claim under test.
    """
    try:
        _raise_chained(SENTINEL_OUTER, SENTINEL_INNER)
    except RuntimeError:
        # .exception() IS exc_info=True, and it is the spelling a library
        # actually uses; the record the formatter receives is the same.
        logging.getLogger(THIRD_PARTY).exception("library_failure")
        raise


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """configure_logging() rewrites the root logger, the dodeal_ai level and
    uvicorn's three loggers, all process-global. Restore every one or a later
    test inherits them."""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    dodeal_logger = logging.getLogger("dodeal_ai")
    original_dodeal_level = dodeal_logger.level
    uvicorn_state = [
        (
            logging.getLogger(n),
            logging.getLogger(n).handlers[:],
            logging.getLogger(n).propagate,
            logging.getLogger(n).level,
        )
        for n in _UVICORN_LOGGERS
    ]
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)
    dodeal_logger.setLevel(original_dodeal_level)
    for logger, handlers, propagate, level in uvicorn_state:
        logger.handlers = handlers
        logger.propagate = propagate
        logger.setLevel(level)


@pytest.fixture
def boom_client():
    """The real app -- real middleware stack, real error handlers -- with one
    extra route, removed again on teardown.

    raise_server_exceptions=False so the 500 path returns the body the
    catch-all built rather than re-raising into the test.
    """
    app.add_api_route(BOOM, sentinel_boom, methods=["GET"])
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.router.routes = [
            route for route in app.router.routes if getattr(route, "path", None) != BOOM
        ]
        app.openapi_schema = None


def _lines(out: str) -> list[dict]:
    """Every JSON line in the captured stdout."""
    return [json.loads(line) for line in out.strip().splitlines() if line.strip()]


def test_no_exception_message_reaches_stdout_through_the_asgi_stack(boom_client, capfd):
    # The whole claim in one test: a foreign, chained exception raised inside a
    # request, under the REAL logging configuration, with the capture at the
    # file descriptor. Neither message may appear anywhere in the bytes.
    configure_logging()

    response = boom_client.get(BOOM)

    out, err = capfd.readouterr()
    assert SENTINEL_OUTER not in out
    assert SENTINEL_INNER not in out
    assert SENTINEL_OUTER not in err
    assert SENTINEL_INNER not in err

    # The frames are still there, and they name the failing route function --
    # what makes the failure debuggable without the messages.
    framed = [line for line in _lines(out) if "exc_frames" in line]
    assert framed, out
    assert any("sentinel_boom" in line["exc_frames"] for line in framed)

    assert response.status_code == 500
    assert SENTINEL_OUTER not in response.text
    assert SENTINEL_INNER not in response.text
    assert response.json()["detail"] == "Internal Server Error"


def test_the_exc_info_key_is_replaced_by_exc_type_and_exc_frames(boom_client, capfd):
    # A collector query on "exc_info" must find nothing: the key is gone, not
    # renamed alongside the old one.
    configure_logging()

    boom_client.get(BOOM)

    out, _ = capfd.readouterr()
    framed = [line for line in _lines(out) if "exc_frames" in line]
    assert framed, out
    for line in framed:
        assert "exc_info" not in line
        assert line["exc_type"] == "builtins.RuntimeError"


def test_a_uvicorn_error_line_carries_no_exception_message(capfd):
    # uvicorn's own logger, handed back to our root handler by
    # configure_logging(), logging exactly as uvicorn does.
    configure_logging()
    try:
        _raise_chained(SENTINEL_OUTER, SENTINEL_INNER)
    except RuntimeError as exc:
        logging.getLogger("uvicorn.error").error("boom", exc_info=exc)

    out, _ = capfd.readouterr()
    assert SENTINEL_OUTER not in out
    assert SENTINEL_INNER not in out

    line = _lines(out)[-1]
    assert line["logger"] == "uvicorn.error"
    assert line["message"] == "boom"
    assert line["exc_type"] == "builtins.RuntimeError"
    assert "_raise_chained" in line["exc_frames"]
    assert "exc_info" not in line


def test_a_chained_cause_contributes_neither_message_nor_frames(capfd):
    # formatException walked __cause__ and printed BOTH messages; frames_only
    # formats the outer traceback only, so the ValueError contributes nothing --
    # not its message, not its class, not its frames.
    configure_logging()
    try:
        _raise_chained(SENTINEL_OUTER, SENTINEL_INNER)
    except RuntimeError as exc:
        logging.getLogger(THIRD_PARTY).error("library_failure", exc_info=exc)

    out, _ = capfd.readouterr()
    assert SENTINEL_INNER not in out
    assert SENTINEL_OUTER not in out
    line = _lines(out)[-1]
    assert "ValueError" not in json.dumps(line)
    assert line["exc_type"] == "builtins.RuntimeError"


def _record(exc_info: object) -> dict:
    """One record formatted by the real JsonFormatter, with exc_info set to
    whatever shape is under test."""
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=exc_info,
    )
    return json.loads(JsonFormatter().format(record))


def test_a_value_none_tuple_yields_the_type_and_no_frames():
    # Some logging paths hand the formatter (type, None, None). There is no
    # exception object to read frames from; the type is still worth having.
    payload = _record((RuntimeError, None, None))
    assert payload["exc_type"] == "builtins.RuntimeError"
    assert "exc_frames" not in payload
    assert "exc_info" not in payload


def test_an_all_none_tuple_yields_neither_field():
    payload = _record((None, None, None))
    assert "exc_type" not in payload
    assert "exc_frames" not in payload
    assert payload["message"] == "boom"


def test_a_bare_true_exc_info_is_ignored_without_raising():
    # exc_info=True before the logging module resolves it: not a shape with
    # frames, and a formatter that raised here would lose the line entirely.
    payload = _record(True)
    assert "exc_type" not in payload
    assert "exc_frames" not in payload
    assert payload["message"] == "boom"
