"""Logging configuration: real JSON output to stdout, dodeal_ai loggers at
INFO (so audit ALLOW lines are not dropped) WITHOUT a caplog level override,
third-party/root loggers unaffected, wired into the app lifespan."""

from __future__ import annotations

import json
import logging

import pytest

from dodeal_ai.core.audit.logger import audit
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_logging_state():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    dodeal_logger = logging.getLogger("dodeal_ai")
    original_dodeal_level = dodeal_logger.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)
    dodeal_logger.setLevel(original_dodeal_level)


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
        tenant="nasir3",
    )

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["decision"] == "allow"
    assert record["level"] == "INFO"
    assert record["logger"] == "dodeal_ai.audit"
    assert record["tenant"] == "nasir3"
    assert record["request_id"] == "req-1"
    assert record["reason_code"] == "ok"


def test_deny_audit_line_is_warning_under_real_config(capsys):
    configure_logging()
    audit(
        decision="deny",
        gate="tenancy",
        request_id="req-2",
        reason_code="tenant_mismatch",
        tenant="nasir3",
    )

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["decision"] == "deny"
    assert record["level"] == "WARNING"


def test_cost_cap_bypassed_reaches_same_json_stream_at_warning(capsys):
    configure_logging()
    logging.getLogger("dodeal_ai.cost").warning(
        "cost_cap_bypassed reason=cost_store_unavailable tenant=%s", "nasir3"
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


def test_configure_logging_called_from_app_lifespan(monkeypatch):
    calls = []
    monkeypatch.setattr("dodeal_ai.main.configure_logging", lambda: calls.append(True))

    from fastapi.testclient import TestClient

    from dodeal_ai.main import app

    with TestClient(app):
        pass
    assert calls == [True]
