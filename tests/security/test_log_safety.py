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

THE DIRECT ROUTE (Piece K, DECISION[DIRECT_ROUTE]) adds a channel this file did
not have to cover before: note text now arrives in a REQUEST BODY on one route,
so it is in scope for the request logger, the validation handler and the
catch-all as well as for the pipeline. The next section drives a sentinel note
through that route on every outcome a request can have -- 200, two different
422s, a 503 and a 500 -- with the handler attached to the ROOT logger at DEBUG,
so "no log line at any level" means every logger in the process and not just
ours.

THE REFUSED REQUEST (Piece L, register item 73) is the sixth outcome and a
different shape from the other five: load shedding answers before the gate chain
and before anything reads the body, so the note is provably somewhere the
service never looked -- and the refusal still writes a WARNING line of its own.
The last section drives the sentinel through that too.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from dodeal_ai.core.audit.logger import audit
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.cost import limiter
from dodeal_ai.core.errors import unhandled_exception_handler
from dodeal_ai.core.llm import LLMErrorReason, LLMProviderError, get_llm_client
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.core.resilience import ExternalCallError, call_with_watchdog
from dodeal_ai.core.validation import OutputValidationError, validate_output
from dodeal_ai.main import app
from dodeal_ai.middleware import inflight
from dodeal_ai.schemas.lead import Lead, LeadNotesResponse
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import (
    CLASSIFY_TEMPLATE,
    classify,
)
from dodeal_ai.units.structured_intelligence.llm_call import parse_output
from dodeal_ai.units.structured_intelligence.schemas import (
    ClassificationOutput,
    NoteType,
)
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_leads import note
from tests.helpers.fake_llm import FakeLLM, json_response, response
from tests.helpers.fake_operational_redis import FakeOperationalRedis

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


class _DeadCostClient:
    """A cost client whose only script execution raises — the Redis outage the
    limiter fails OPEN on."""

    async def eval(self, *args, **kwargs):
        raise limiter.redis.RedisError("down")


async def test_cost_bypass_keeps_the_tenant_in_a_field_not_in_the_message(
    log_capture, settings_env, monkeypatch
):
    # Not note content, but the same rule: an identifier belongs in a FIELD a
    # collector can filter, never interpolated into prose under "message".
    # This line is what a spend-cap alert fires on, so its shape is a contract.
    monkeypatch.setattr(limiter, "get_cost_client", lambda: _DeadCostClient())

    await limiter.enforce_cost("tenant-b", "42")  # must NOT raise

    line = next(x for x in _lines(log_capture) if x["message"] == "cost_cap_bypassed")
    assert line["level"] == "WARNING"
    assert line["logger"] == "dodeal_ai.cost"
    assert line["reason_code"] == "cost_store_unavailable"
    assert line["tenant"] == "tenant-b"
    # The tenant label is nowhere in the message text.
    assert "tenant-b" not in line["message"]


# --- model output: the string a model wrote after reading a note ------------


def test_unparseable_model_output_logs_no_model_text(log_capture):
    # The decode failure. A JSONDecodeError carries the whole offending document
    # on .doc and quotes a slice of it in its message, so neither the exception
    # nor the log line may be built from it.
    with pytest.raises(OutputValidationError) as raised:
        parse_output(
            response(f"I would say this note is about {SENTINEL}"),
            ClassificationOutput,
            "llm.unit_a.classify",
        )

    _assert_sentinel_absent(log_capture)
    assert SENTINEL not in str(raised.value)

    line = next(
        x for x in _lines(log_capture) if x["message"].startswith("output_validation")
    )
    assert "error_types=json_invalid" in line["message"]


def test_wrongly_shaped_model_output_logs_no_model_text(log_capture):
    # The schema failure. Valid JSON, invalid shape -- and the invented field's
    # VALUE is model output that pydantic would otherwise quote as input_value.
    with pytest.raises(OutputValidationError):
        parse_output(
            json_response({"note_type": "discovery", "reasoning": SENTINEL}),
            ClassificationOutput,
            "llm.unit_a.classify",
        )

    _assert_sentinel_absent(log_capture)


def test_a_note_shaped_model_answer_is_not_echoed_by_the_error(log_capture):
    # The worst case: the model repeats the note back at us instead of
    # answering. That string is note text wearing model output's clothes.
    with pytest.raises(OutputValidationError) as raised:
        parse_output(
            json_response({"note_type": SENTINEL}),
            ClassificationOutput,
            "llm.unit_a.classify",
        )

    _assert_sentinel_absent(log_capture)
    assert SENTINEL not in str(raised.value)
    assert SENTINEL not in repr(raised.value.errors)


# --- the resubmission reference: a fingerprint stored as a VALUE ------------


async def test_the_resubmission_reference_never_reaches_a_log_line(
    log_capture, monkeypatch
):
    # Register item 33 is the one place this service stores a note fingerprint
    # as a VALUE rather than inside a key. A digest in the log stream is a
    # stable identifier for one specific note body -- it does not read as note
    # text, which is exactly why it could slip past a reviewer -- and the store
    # failure path is where a value would be interpolated by mistake.
    fingerprint = state.note_fingerprint(SENTINEL)
    monkeypatch.setattr(
        state,
        "get_operational_client",
        lambda: FakeOperationalRedis(raise_on={"set", "get"}),
    )

    await state.write_attempt_fingerprint(
        "tenant-a", 1656, 10, fingerprint, ttl=21600, request_id="req-sentinel"
    )
    read = await state.read_attempt_fingerprint(
        "tenant-a", 1656, 10, request_id="req-sentinel"
    )

    assert read is None  # fails OPEN, with no new bypass code
    _assert_sentinel_absent(log_capture)
    assert fingerprint not in log_capture.getvalue()
    assert "attempt_fp:" not in log_capture.getvalue()

    line = _lines(log_capture)[-1]
    assert line["reason_code"] == "attempt_counter_bypassed"


# --- the reprompt: a rejected answer is written down twice, and neither ------
# --- time is it quoted ------------------------------------------------------


async def test_a_reprompt_logs_the_label_and_never_the_rejected_answer(
    log_capture, settings_env
):
    # The reprompt is the one path that handles a rejected answer TWICE: once to
    # reject it, once to decide to ask again. Both lines are emitted here, and
    # the second is the newer of the two.
    client = FakeLLM(
        response(f"I would say this note is about {SENTINEL}"),
        json_response({"note_type": "discovery"}),
    )
    await classify(client, note(10, "A note."), Lead(id=1), settings=get_settings())

    assert client.call_count == 2
    _assert_sentinel_absent(log_capture)

    line = next(x for x in _lines(log_capture) if x["message"] == "reprompt_issued")
    assert line["level"] == "WARNING"
    assert line["reason_code"] == "reprompt_issued"
    assert line["label"] == "llm.unit_a.classify"


async def test_the_rejected_answer_is_not_carried_into_the_second_prompt(
    log_capture, settings_env
):
    # Not a log line, but the same rule and the same reason: the prompt is the
    # other place a rejected answer could be written down. `.variable` is
    # excluded from repr precisely because it must never be printable, so this
    # asserts on `.text`, which is what would actually be sent.
    client = FakeLLM(
        response(f"the note said {SENTINEL}"),
        json_response({"note_type": "discovery"}),
    )
    await classify(client, note(10, "A note."), Lead(id=1), settings=get_settings())

    second = client.prompts[1].text
    assert SENTINEL not in second
    assert "0501234567" not in second
    _assert_sentinel_absent(log_capture)


# --- the direct route: note text in a request body (DECISION[DIRECT_ROUTE]) --
#
# Five outcomes, one sentinel. The route accepts the saved note in the body, so
# every path a request can take out of it is a path the text could leak on: the
# 200 (the pipeline logs an outcome line), the two 422s (the validation handler
# sees the body), the 503 (the model failed with the text in flight) and the 500
# (an unexpected exception, whose traceback is formatted and logged).


DIRECT = "/api/v1/notes/judgements/direct"

SENTINEL_PROJECT = "SENTINEL-PROJECT-Marassi-Plot-0501234567"


def _raise_boom(*args: object, **kwargs: object):
    """Raise from a frame whose SOURCE contains no sentinel.

    Same reason as _raise_foreign above: a traceback quotes the source text of
    every frame it lists, so the forced failure has to be raised somewhere the
    sentinel is not written down.
    """
    raise RuntimeError("boom")


class _DirectCostRedis:
    async def eval(self, script, numkeys, *keys_and_args):
        keys = keys_and_args[:numkeys]
        amount = int(keys_and_args[numkeys])
        return [amount for _ in keys]


@pytest.fixture
def root_log_capture():
    """The real JsonFormatter on the ROOT logger at DEBUG.

    Broader than `log_capture` in two ways, both deliberate: DEBUG rather than
    INFO, so "at any level" is literal, and the root logger rather than the
    `dodeal_ai` tree, so a line written by starlette, httpx or anything else in
    the process is captured too. The claim being tested is that the note text is
    in NO log line, not that it is in none of ours.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    original_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)


@pytest.fixture
def direct_client(monkeypatch):
    """The direct route behind the real gate chain, with every seam faked."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )
    llm = FakeLLM()
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": "discovery"}))
    llm.script_for(
        template_for(NoteType.DISCOVERY),
        json_response(
            {
                "is_vague": True,
                "missing_components": ["next_step_with_date"],
                "clarification_prompt": "When are you following up?",
                "reasoning": "No date was given.",
            }
        ),
    )
    llm.script_for(
        SCORE_TEMPLATE,
        json_response(
            {
                "marks": {
                    "what_happened": 20,
                    "client_said": 15,
                    "next_step_date": 15,
                    "clarity": 5,
                }
            }
        ),
    )

    monkeypatch.setattr(limiter, "get_cost_client", lambda: _DirectCostRedis())
    monkeypatch.setattr(state, "get_operational_client", lambda: FakeOperationalRedis())
    monkeypatch.setattr(limiter, "_TOKEN_PREFLIGHT_LOGGED", False)

    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    app.dependency_overrides[get_llm_client] = lambda: llm

    # raise_server_exceptions=False so the 500 path returns the shaped body the
    # catch-all built, rather than re-raising into the test.
    yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _fail_the_model() -> None:
    """Swap in a client whose FIRST call raises a provider failure.

    A fresh FakeLLM with a POSITIONAL script rather than a re-script of the
    fixture's: `script_for` appends to a template's queue, so queueing a failure
    behind the good answer would let the good answer serve the call and the
    request would come back 200.
    """
    app.dependency_overrides[get_llm_client] = lambda: FakeLLM(
        LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True)
    )


def _direct_headers() -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_token(subdomain='tenant-a', sub=42)}",
        "Host": "tenant-a.dodealcrm.com",
    }


def _direct_body(text: str = SENTINEL, project: str | None = None) -> dict:
    return {
        "lead_id": 1656,
        "note_id": 10,
        "author_id": 27,
        "note_text": text,
        "lead": {"leadType": "buyer", "enquiryType": "sale", "project": project},
    }


def _assert_project_sentinel_absent(stream: io.StringIO) -> None:
    text = stream.getvalue()
    assert "SENTINEL-PROJECT" not in text
    assert "Marassi" not in text


# --- the note itself, on all five outcomes ----------------------------------


def test_direct_happy_path_logs_no_note_text(direct_client, root_log_capture):
    # The note is judged and an outcome line is written. That line is the one
    # most likely to leak a body, because it is the line that always happens.
    body = _direct_body(SENTINEL + " Following up Tuesday at 3pm.")
    r = direct_client.post(DIRECT, json=body, headers=_direct_headers())

    assert r.status_code == 200
    assert r.json()["suppressed"] is None
    _assert_sentinel_absent(root_log_capture)
    assert SENTINEL not in r.text


def test_direct_extra_field_422_logs_no_note_text(direct_client, root_log_capture):
    # The validation handler's own case, on the route that carries text: it
    # drops every value, so the body reaches neither the response nor the line.
    body = {**_direct_body(), "note": SENTINEL}
    r = direct_client.post(DIRECT, json=body, headers=_direct_headers())

    assert r.status_code == 422
    assert r.json()["reason"] == "invalid_request"
    _assert_sentinel_absent(root_log_capture)
    assert SENTINEL not in r.text

    line = next(
        x
        for x in _lines(root_log_capture)
        if x.get("message") == "request_validation_failed"
    )
    assert line["error_types"] == "extra_forbidden"


def test_direct_over_length_422_logs_no_note_text(direct_client, root_log_capture):
    # The hard ceiling. pydantic's string_too_long error carries the offending
    # string as `input`; the handler drops it, and this is the path where that
    # value is a whole note.
    over = SENTINEL + "x" * 4000
    r = direct_client.post(DIRECT, json=_direct_body(over), headers=_direct_headers())

    assert r.status_code == 422
    _assert_sentinel_absent(root_log_capture)
    assert SENTINEL not in r.text

    line = next(
        x
        for x in _lines(root_log_capture)
        if x.get("message") == "request_validation_failed"
    )
    assert line["error_types"] == "string_too_long"


def test_direct_model_failure_503_logs_no_note_text(direct_client, root_log_capture):
    # The model failed with the note in flight. The provider error is ours, so
    # its fixed-vocabulary message is kept -- and the note is not in it.
    _fail_the_model()
    r = direct_client.post(
        DIRECT,
        json=_direct_body(SENTINEL + " Following up Tuesday."),
        headers=_direct_headers(),
    )

    assert r.status_code == 503
    assert r.json()["reason"] == "model_unavailable"
    _assert_sentinel_absent(root_log_capture)
    assert SENTINEL not in r.text


def test_direct_unexpected_error_500_logs_no_note_text(
    direct_client, root_log_capture, monkeypatch
):
    # The catch-all, with the note in the request body. The traceback is
    # formatted and logged frames-only, and the frames run through the pipeline
    # while it is holding the note -- so this is the path where a `%r` of a
    # local, or a chained message, would take the whole body with it.
    monkeypatch.setattr(state, "read_rate_limit", _raise_boom)

    r = direct_client.post(
        DIRECT,
        json=_direct_body(SENTINEL + " Following up Tuesday."),
        headers=_direct_headers(),
    )

    assert r.status_code == 500
    _assert_sentinel_absent(root_log_capture)
    assert SENTINEL not in r.text

    line = next(
        x for x in _lines(root_log_capture) if x.get("message") == "unhandled_exception"
    )
    assert line["error_type"] == "RuntimeError"
    assert "error" not in line  # a foreign exception's message IS the data


# --- the lead context, on the same paths ------------------------------------
#
# `lead.project` is caller-supplied text on the same body and it reaches the
# classifier's prompt, so it is note-shaped data by any other name: a project
# name is a customer's purchase, and it must not reach a log line either.


def test_direct_happy_path_logs_no_lead_project(direct_client, root_log_capture):
    r = direct_client.post(
        DIRECT,
        json=_direct_body("Called the client, discussed the plot.", SENTINEL_PROJECT),
        headers=_direct_headers(),
    )

    assert r.status_code == 200
    _assert_project_sentinel_absent(root_log_capture)


def test_direct_extra_field_422_logs_no_lead_project(direct_client, root_log_capture):
    body = {**_direct_body("Called the client.", SENTINEL_PROJECT), "note": "x"}
    r = direct_client.post(DIRECT, json=body, headers=_direct_headers())

    assert r.status_code == 422
    _assert_project_sentinel_absent(root_log_capture)
    assert "SENTINEL-PROJECT" not in r.text


def test_direct_model_failure_503_logs_no_lead_project(direct_client, root_log_capture):
    _fail_the_model()
    r = direct_client.post(
        DIRECT,
        json=_direct_body("Called the client, discussed the plot.", SENTINEL_PROJECT),
        headers=_direct_headers(),
    )

    assert r.status_code == 503
    _assert_project_sentinel_absent(root_log_capture)


def test_direct_unexpected_error_500_logs_no_lead_project(
    direct_client, root_log_capture, monkeypatch
):
    monkeypatch.setattr(state, "read_rate_limit", _raise_boom)

    r = direct_client.post(
        DIRECT,
        json=_direct_body("Called the client, discussed the plot.", SENTINEL_PROJECT),
        headers=_direct_headers(),
    )

    assert r.status_code == 500
    _assert_project_sentinel_absent(root_log_capture)


# --- the refused request: a body that was never even read -------------------
#
# Load shedding (register item 73) refuses at the door, before the gate chain
# and before anything reads the body. That makes it the ONE path where a note
# can be in a request and provably nowhere else -- which is exactly why it needs
# a test: the refusal writes a WARNING line of its own, and a line written about
# a request nobody looked at is the easiest place to start looking at it.


@pytest.fixture
def _shed_everything(monkeypatch):
    """A cap of zero, so the very next request is refused.

    Zero is refused by `Settings` itself (a cap of 0 is a config typo that looks
    like an outage), so the cap is stubbed at the read site rather than set --
    which is also the honest shape of the test: what is being driven is the
    refusal branch, not the configuring of it.
    """

    class _Full:
        max_inflight = 0

    monkeypatch.setattr(inflight, "get_settings", lambda: _Full())


def test_a_shed_request_body_reaches_no_log_line(
    direct_client, root_log_capture, _shed_everything
):
    r = direct_client.post(
        DIRECT,
        json=_direct_body(SENTINEL + " Following up Tuesday."),
        headers=_direct_headers(),
    )

    assert r.status_code == 503
    assert r.json()["reason"] == "load_shed"
    _assert_sentinel_absent(root_log_capture)
    assert SENTINEL not in r.text


def test_a_shed_request_lead_project_reaches_no_log_line(
    direct_client, root_log_capture, _shed_everything
):
    r = direct_client.post(
        DIRECT,
        json=_direct_body("Called the client, discussed the plot.", SENTINEL_PROJECT),
        headers=_direct_headers(),
    )

    assert r.status_code == 503
    _assert_project_sentinel_absent(root_log_capture)


def test_the_shed_line_carries_no_tenant_and_no_body(
    direct_client, root_log_capture, _shed_everything
):
    # The request carried a valid token for tenant-a and a note in its body. The
    # line names neither: the gates have not run, so the tenant would be the
    # caller's own unverified claim, and the body was never read.
    r = direct_client.post(
        DIRECT,
        json=_direct_body(SENTINEL + " Following up Tuesday."),
        headers=_direct_headers(),
    )

    assert r.status_code == 503
    line = next(x for x in _lines(root_log_capture) if x.get("message") == "load_shed")
    assert line["reason_code"] == "load_shed"
    assert "tenant" not in line
    assert "note_text" not in line
    assert "tenant-a" not in json.dumps(line)
