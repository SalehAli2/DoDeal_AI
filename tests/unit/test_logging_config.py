"""Logging configuration: real JSON output to stdout, dodeal_ai loggers at
INFO (so audit ALLOW lines are not dropped) WITHOUT a caplog level override,
third-party/root loggers unaffected, wired into the app lifespan."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from dodeal_ai.core.audit.logger import audit
from dodeal_ai.core.logging_config import _UVICORN_LOGGERS, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging_state():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    dodeal_logger = logging.getLogger("dodeal_ai")
    original_dodeal_level = dodeal_logger.level
    # configure_logging() now also rewrites uvicorn's three loggers; they are
    # process-global, so restore them too or a later test inherits them.
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


def test_allow_audit_line_emitted_under_real_config(capsys):
    # No caplog level override anywhere in this test -- this is the real
    # configured pipeline, proving INFO actually reaches stdout, flat
    # (not nested), with the exact existing field set.
    configure_logging()
    audit(
        decision="allow",
        gate="auth",
        request_id="req-1",
        reason_code="ok",
        tenant="tenant-a",
    )

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["decision"] == "allow"
    assert record["level"] == "INFO"
    assert record["logger"] == "dodeal_ai.audit"
    assert record["tenant"] == "tenant-a"
    assert record["request_id"] == "req-1"
    assert record["reason_code"] == "ok"


def test_deny_audit_line_is_warning_under_real_config(capsys):
    configure_logging()
    audit(
        decision="deny",
        gate="tenancy",
        request_id="req-2",
        reason_code="tenant_mismatch",
        tenant="tenant-a",
    )

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["decision"] == "deny"
    assert record["level"] == "WARNING"


def test_cost_cap_bypassed_reaches_same_json_stream_at_warning(capsys):
    configure_logging()
    logging.getLogger("dodeal_ai.cost").warning(
        "cost_cap_bypassed reason=cost_store_unavailable tenant=%s", "tenant-a"
    )

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["level"] == "WARNING"
    assert record["logger"] == "dodeal_ai.cost"
    assert "cost_cap_bypassed" in record["message"]


def test_third_party_logger_stays_at_warning(capsys):
    configure_logging()
    logging.getLogger("some_third_party_lib").info("should not appear")
    assert capsys.readouterr().out == ""


def test_uvicorn_access_line_arrives_as_json_on_the_same_stream(capsys):
    # L6: uvicorn configures its own loggers with a plain-text handler and
    # propagate=False, so its lines used to land on stdout in a SECOND format
    # that no collector could index alongside ours. Reproduce that setup, then
    # configure_logging() must hand the logger back to the root JSON handler.
    access = logging.getLogger("uvicorn.access")
    access.handlers = [logging.StreamHandler(stream=sys.stdout)]
    access.propagate = False
    access.setLevel(logging.INFO)  # what uvicorn itself sets

    configure_logging()

    assert access.handlers == []
    assert access.propagate is True
    assert access.level == logging.INFO  # level left exactly as uvicorn set it

    access.info('127.0.0.1:0 - "GET /health HTTP/1.1" 200')

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1  # ONE line, not one JSON + one plain-text copy
    record = json.loads(out[0])
    assert record["logger"] == "uvicorn.access"
    assert record["level"] == "INFO"
    assert "GET /health" in record["message"]


@pytest.mark.parametrize("name", _UVICORN_LOGGERS)
def test_every_uvicorn_logger_is_handed_to_the_root_handler(name):
    logger = logging.getLogger(name)
    logger.handlers = [logging.StreamHandler(stream=sys.stdout)]
    logger.propagate = False

    configure_logging()

    assert logger.handlers == []
    assert logger.propagate is True


def test_configure_logging_called_from_app_lifespan(monkeypatch):
    calls = []
    monkeypatch.setattr("dodeal_ai.main.configure_logging", lambda: calls.append(True))

    from fastapi.testclient import TestClient

    from dodeal_ai.main import app

    with TestClient(app):
        pass
    assert calls == [True]
