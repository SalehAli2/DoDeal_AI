"""Startup signal for a misprovisioned deployment (audit finding F2): an empty
dd_api_keys map means nothing can reach the backend, and that must be loud at
startup, not discovered later as every data call failing.

WHY THESE READ THE JSON STREAM AND NOT `caplog`. The line is emitted AFTER
`configure_logging()` (audit §6.9) so that it goes out as a formatted JSON
object like every other startup event, rather than through whatever handler
`logging` happened to have. But `configure_logging()` does `root.handlers =
[handler]` -- it REPLACES the root's handlers -- and `caplog` works by
installing its own handler on the root. So caplog's handler is evicted during
startup and sees nothing, even though the record was emitted correctly.

Attaching to the `dodeal_ai` logger instead survives, because
`configure_logging()` sets that logger's LEVEL and never touches its handlers.
It is also the more honest test: it asserts the shape a log collector actually
receives, not the shape an in-process capture fixture reconstructs.

Same pattern as `json_capture` in tests/unit/test_judgement_pipeline.py and
`log_capture` in tests/security/test_unit_a_injection.py.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app

STARTUP_LOGGER = "dodeal_ai.startup"
EVENT = "backend_keys_missing"


@pytest.fixture
def json_lines():
    """The real JsonFormatter over the dodeal_ai tree, as parsed objects.

    Attached to `dodeal_ai` rather than to the root, so it survives the
    `configure_logging()` that runs inside the app's lifespan.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield lambda: [json.loads(x) for x in stream.getvalue().splitlines()]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def _events(lines: list[dict], name: str) -> list[dict]:
    """Every captured line whose message names `name`. The formatter has no
    dedicated event field -- the event IS the message, and this unit's messages
    are a fixed vocabulary with the code first."""
    return [line for line in lines if name in str(line.get("message", ""))]


def test_backend_keys_missing_logs_error_when_map_is_empty(monkeypatch, json_lines):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.delenv("DODEAL_DD_API_KEYS", raising=False)
    get_settings.cache_clear()

    with TestClient(app):
        pass

    found = _events(json_lines(), EVENT)
    assert found, "no backend_keys_missing line reached the JSON stream"
    # A real JSON object on the collector's stream, at ERROR, from the startup
    # logger -- which is the whole point of emitting it after configure_logging.
    assert found[0]["level"] == "ERROR"
    assert found[0]["logger"] == STARTUP_LOGGER
    get_settings.cache_clear()


def test_backend_keys_missing_not_logged_when_map_is_non_empty(monkeypatch, json_lines):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_DD_API_KEYS", '{"tenant-a":"a-real-key"}')
    get_settings.cache_clear()

    with TestClient(app):
        pass

    # PROVE THE CHANNEL WORKS FIRST. An absence assertion over a capture that
    # sees nothing passes for the wrong reason -- which is exactly how this
    # test survived the §6.9 move while its partner failed. The probe is logged
    # after startup, so it goes through the same post-configure_logging channel
    # the real line would have used.
    logging.getLogger(STARTUP_LOGGER).error("startup_probe_line count=0")

    lines = json_lines()
    assert _events(lines, "startup_probe_line"), (
        "the capture channel saw nothing at all, so the absence assertion below "
        "would pass vacuously"
    )
    assert _events(lines, EVENT) == []
    get_settings.cache_clear()
