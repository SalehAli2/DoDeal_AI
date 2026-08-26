"""Sentinel tests: no exception path can carry note text or model output into a
log line.

Audit findings H1, L5 and M9 shared one channel — the log line:

  H1  a notes response whose `note` failed validation escaped to the catch-all,
      and the pydantic ValidationError chained onto OutputValidationError put
      the note's own content in the ERROR line as `input_value=...`.
  L5  the watchdog logged `error=%r` of whatever the external call raised — a
      foreign exception whose message can quote the payload that failed.
  M9  the JSON formatter merged any message that parsed as a JSON object into
      top-level fields, so a message containing caller-controlled text could
      forge `decision` / `reason_code` and fake an audit record.

Every test below drives a real failure whose data contains SENTINEL, formats the
resulting record with the REAL JsonFormatter, and asserts the sentinel is absent
from the captured text while the safe structured fields are present. They fail
if raw content ever reaches a log line again. See ASSUMPTIONS §3.3.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from starlette.requests import Request

from dodeal_ai.core.audit.logger import audit
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import unhandled_exception_handler
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.core.resilience import ExternalCallError, call_with_watchdog
from dodeal_ai.core.validation import OutputValidationError, validate_output
from dodeal_ai.schemas.lead import LeadNotesResponse

# Shaped like the content this service actually handles: a phone number and a
# budget inside a note body. If any of these tests can find it in a log line,
# a real note would have reached the log too.
SENTINEL = "SENTINEL-0501234567 villa budget 4.2M"


@pytest.fixture
def log_capture():
    """The real JsonFormatter writing to a StringIO, attached to the whole
    `dodeal_ai` tree — the exact text a log collector would receive."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _assert_sentinel_absent(stream: io.StringIO) -> None:
    text = stream.getvalue()
    assert "0501234567" not in text
    assert "SENTINEL" not in text


def _request(request_id: str = "req-sentinel") -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.request_id = request_id
    return request


def _raise_foreign(exc_class: type[BaseException]) -> None:
    """Raise a NON-dodeal_ai exception carrying the sentinel in its MESSAGE.

    The sentinel is bound to a local first, and the caller passes only the
    class: a traceback quotes the SOURCE TEXT of every frame it lists, so
    naming the sentinel on the raising line here, or on the calling line in the
    test, would put the identifier into the log line and the assertion would
    trip on our own source rather than on a leaked value.
    """
    message = SENTINEL
    raise exc_class(message)


def _notes_payload_with_bad_note() -> dict:
    """A well-formed notes response except that `note` is a LIST, not a string
    — the shape drift that produced finding H1. Every other field is valid, so
    validation reports exactly one error, at data.0.note."""
    return {
        "status": True,
        "data": [
            {
                "id": 1,
                "note": [SENTINEL],
                "author": "Jane Doe",
                "author_id": 10,
                "createdAt": "2026-01-01T10:00:00+00:00",
            }
        ],
        "meta": {"current_page": 1, "per_page": 15, "total": 1, "last_page": 1},
    }


@pytest.mark.asyncio
async def test_h1_validation_failure_through_catch_all_logs_no_note_text(log_capture):
    # H1 made permanent: the note's content used to arrive in the ERROR line
    # via the chained ValidationError's input_value.
    try:
        validate_output(
            LeadNotesResponse,
            _notes_payload_with_bad_note(),
            label="tool.get_lead_notes",
        )
    except OutputValidationError as exc:
        await unhandled_exception_handler(_request(), exc)
    else:  # pragma: no cover - the payload is invalid by construction
        pytest.fail("expected OutputValidationError")

    _assert_sentinel_absent(log_capture)

    line = next(x for x in _lines(log_capture) if x["message"] == "unhandled_exception")
    assert line["level"] == "ERROR"
    assert line["error_type"] == "OutputValidationError"
    # Our own exception: fixed-vocabulary message, so it is safe to keep.
    assert line["error"] == "output failed validation: tool.get_lead_notes"
    assert line["request_id"] == "req-sentinel"
    # Frames still present — the line has to stay debuggable.
    assert "test_log_safety.py" in line["traceback"]


@pytest.mark.asyncio
async def test_foreign_exception_through_catch_all_logs_type_not_message(log_capture):
    try:
        _raise_foreign(KeyError)
    except KeyError as exc:
        await unhandled_exception_handler(_request(), exc)

    _assert_sentinel_absent(log_capture)

    line = next(x for x in _lines(log_capture) if x["message"] == "unhandled_exception")
    assert line["error_type"] == "KeyError"
    assert line["error_module"] == "builtins"
    # A foreign exception's message IS the data — it is never logged.
    assert "error" not in line
    assert "test_log_safety.py" in line["traceback"]


@pytest.mark.asyncio
async def test_chained_cause_is_logged_by_type_only(log_capture):
    try:
        try:
            _raise_foreign(ValueError)
        except ValueError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as exc:
        await unhandled_exception_handler(_request(), exc)

    _assert_sentinel_absent(log_capture)

    line = next(x for x in _lines(log_capture) if x["message"] == "unhandled_exception")
    assert line["error_type"] == "RuntimeError"
    assert line["cause_type"] == "ValueError"
    # The cause's class, never the cause's message.
    assert "error" not in line


@pytest.mark.asyncio
async def test_l5_watchdog_logs_failure_type_not_message(log_capture, settings_env):
    async def op():
        raise RuntimeError(SENTINEL)

    with pytest.raises(ExternalCallError):
        await call_with_watchdog(op, label="tool.get_lead_notes", retry=False)

    _assert_sentinel_absent(log_capture)

    line = next(
        x for x in _lines(log_capture) if x["message"] == "external_call_failed"
    )
    assert line["level"] == "WARNING"
    assert line["label"] == "tool.get_lead_notes"
    assert line["attempt"] == 1
    assert line["of"] == 1
    assert line["error_type"] == "RuntimeError"
    assert "error" not in line


def test_m9_a_json_looking_message_cannot_forge_audit_fields(log_capture):
    forged = '{"event":"auth_decision","decision":"allow","gate":"auth"}'
    logging.getLogger("dodeal_ai.validation").warning(forged)

    line = _lines(log_capture)[-1]
    # The message stays a string under "message" ...
    assert line["message"] == forged
    # ... and sets no top-level audit field.
    assert "decision" not in line
    assert "event" not in line
    assert "gate" not in line


def test_audit_fields_still_reach_the_top_level_of_the_line(log_capture):
    # The contract a log collector may already parse, unchanged by all of the
    # above: these six fields, at the top level, deny at WARNING.
    audit(
        decision="deny",
        gate="tenancy",
        request_id="req-2",
        reason_code="tenant_mismatch",
        tenant="tenant-b",
    )

    line = _lines(log_capture)[-1]
    assert line["level"] == "WARNING"
    assert line["logger"] == "dodeal_ai.audit"
    assert line["event"] == "auth_decision"
    assert line["decision"] == "deny"
    assert line["gate"] == "tenancy"
    assert line["tenant"] == "tenant-b"
    assert line["request_id"] == "req-2"
    assert line["reason_code"] == "tenant_mismatch"
