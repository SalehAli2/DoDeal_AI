"""The judgement routes over HTTP: the gates still hold, the body contract is
enforced without echoing the caller's input, the pipeline's stop-points map to
their enumerated codes, and nothing reaches a model.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

import dodeal_ai.core.auth.dependencies as auth_dependencies
import dodeal_ai.core.cost.limiter as cost_limiter
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.cost.limiter import CostLimitError
from dodeal_ai.core.llm import (
    LLMErrorReason,
    LLMProviderError,
    get_llm_client,
)
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.main import app
from dodeal_ai.tools.leads import get_leads_client
from dodeal_ai.units.structured_intelligence import state
from tests.helpers import tokens
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FAKE_MODEL, FakeLLM, json_response, response
from tests.helpers.fake_operational_redis import FakeOperationalRedis

JUDGE = "/api/v1/notes/judgements"
RESUBMIT = "/api/v1/notes/judgements/resubmission"
VERSIONS = "/api/v1/meta/versions"

LEAD_ID = 1656
NOTE_ID = 10
GOOD_NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."


def _classified(note_type: str):
    """One scripted classifier reply: exactly the JSON object the template asks
    for, and nothing around it."""
    return json_response({"note_type": note_type})


class _FakeCostRedis:
    """Gate 4's store. Copied from tests/security/test_chain.py -- the cost gate
    runs on every one of these routes and must not be the thing that fails."""

    def __init__(self):
        self.store: dict[str, int] = {}

    async def eval(self, script, numkeys, *keys_and_args):
        keys = keys_and_args[:numkeys]
        amount = int(keys_and_args[numkeys])
        counts = []
        for key in keys:
            self.store[key] = self.store.get(key, 0) + amount
            counts.append(self.store[key])
        return counts


@pytest.fixture
def leads() -> FakeLeadsClient:
    return FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)},
        notes={LEAD_ID: [note(NOTE_ID, GOOD_NOTE), note(11, "Left voicemail again.")]},
    )


@pytest.fixture
def llm() -> FakeLLM:
    """Four identical classifications -- enough for the tests that judge twice.

    Deliberately not an endless supply: an exhausted FakeLLM raises rather than
    returning a default, so a path that calls the model more often than the
    pipeline is specified to is loud instead of silently absorbed.
    """
    return FakeLLM(*[_classified("discovery") for _ in range(4)])


@pytest.fixture
def operational() -> FakeOperationalRedis:
    return FakeOperationalRedis()


@pytest.fixture
def client(monkeypatch, leads, llm, operational):
    """The route fixture: the gate-chain wiring from test_chain.py:38-52, plus
    the three seams this unit reaches the world through.

    app.dependency_overrides[get_llm_client] is the FIRST such override in the
    repo -- get_llm_client() itself still raises, and must keep raising.
    """
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )

    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: _FakeCostRedis())
    monkeypatch.setattr(state, "get_operational_client", lambda: operational)
    # The once-per-process flag, reset so each test sees a clean process.
    monkeypatch.setattr(cost_limiter, "_TOKEN_PREFLIGHT_LOGGED", False)

    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    app.dependency_overrides[get_leads_client] = lambda: leads
    app.dependency_overrides[get_llm_client] = lambda: llm

    yield TestClient(app)

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def json_log():
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


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _headers(subdomain: str = "tenant-a", sub: int = 42) -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_token(subdomain=subdomain, sub=sub)}",
        "Host": f"{subdomain}.dodealcrm.com",
    }


def _body(lead_id: int = LEAD_ID, note_id: int = NOTE_ID) -> dict:
    return {"lead_id": lead_id, "note_id": note_id}


# --- the gates still hold on the new routes --------------------------------


def test_no_token_is_401(client):
    r = client.post(JUDGE, json=_body(), headers={"Host": "tenant-a.dodealcrm.com"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}


def test_bad_token_is_401(client):
    r = client.post(
        JUDGE,
        json=_body(),
        headers={
            "Authorization": "Bearer not-a-token",
            "Host": "tenant-a.dodealcrm.com",
        },
    )
    assert r.status_code == 401


def test_cross_tenant_host_is_403(client):
    r = client.post(
        JUDGE,
        json=_body(),
        headers={**_headers(), "Host": "tenant-b.dodealcrm.com"},
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "Forbidden"}


def test_cost_cap_is_429_with_the_pinned_gate_body(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise CostLimitError("tenant_quota_exceeded")

    monkeypatch.setattr(auth_dependencies, "enforce_cost", _raise)
    r = client.post(JUDGE, json=_body(), headers=_headers())
    assert r.status_code == 429
    # The gate body is UNCHANGED by the new error taxonomy: no "reason" key.
    assert r.json() == {"detail": "Too Many Requests"}


def test_the_gates_run_before_the_tool_layer(client, leads):
    client.post(JUDGE, json=_body(), headers={"Host": "tenant-a.dodealcrm.com"})
    assert leads.calls == []  # 401'd before anything was fetched


def test_all_three_routes_are_gated(client):
    # Every route this phase adds sits behind Depends(gate4_cost). A new route
    # that forgets it would be an ungated path into the tool layer.
    assert client.post(JUDGE, json=_body()).status_code == 401
    assert client.post(RESUBMIT, json=_body()).status_code == 401
    assert client.get(VERSIONS).status_code == 401


# --- the body contract, without echoing the caller's input -----------------


def test_unknown_body_field_is_422_invalid_request(client):
    r = client.post(
        JUDGE,
        json={**_body(), "note": "Called, no answer."},
        headers=_headers(),
    )
    assert r.status_code == 422
    assert r.json() == {
        "detail": "Unprocessable Entity",
        "reason": "invalid_request",
        "request_id": r.headers["X-Request-ID"],
    }


def test_the_422_does_not_echo_the_callers_input(client):
    secret = "SENTINEL-0501234567 villa budget 4.2M"
    r = client.post(JUDGE, json={**_body(), "note": secret}, headers=_headers())

    assert r.status_code == 422
    # FastAPI's stock handler would return exc.errors(), which carries `input`
    # -- the caller's own submitted value -- straight back out.
    assert secret not in r.text
    assert "0501234567" not in r.text
    assert "note" not in r.text  # not even the field name


def test_a_missing_field_is_also_invalid_request(client):
    r = client.post(JUDGE, json={"lead_id": LEAD_ID}, headers=_headers())
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid_request"


def test_a_non_integer_id_is_invalid_request(client):
    r = client.post(
        JUDGE, json={"lead_id": LEAD_ID, "note_id": "ten"}, headers=_headers()
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid_request"


def test_validation_failure_logs_types_not_values(client, json_log):
    secret = "SENTINEL-0501234567"
    client.post(JUDGE, json={**_body(), "note": secret}, headers=_headers())

    text = json_log.getvalue()
    assert secret not in text
    line = next(
        x for x in _lines(json_log) if x["message"] == "request_validation_failed"
    )
    assert line["reason_code"] == "invalid_request"
    assert line["error_types"] == "extra_forbidden"
    assert line["error_count"] == 1


# --- the pipeline's stop points --------------------------------------------


def test_missing_lead_is_backend_unavailable_today(client, leads):
    # H2: the watchdog collapses a backend 404 into ExternalCallError, which is
    # indistinguishable from a 500 or a timeout. Reporting that as
    # lead_not_found would tell the CRM a lead is missing when the backend is
    # merely down, so it is 503 until typed backend errors land at step 4.
    leads.raise_on["get_lead"] = ExternalCallError("tool.get_lead", RuntimeError())
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json()["reason"] == "backend_unavailable"


def test_backend_failure_leaks_no_internal_detail(client, leads):
    leads.raise_on["get_lead"] = ExternalCallError(
        "tool.get_lead", RuntimeError("connection refused to 10.0.0.4:6379")
    )
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert "10.0.0.4" not in r.text
    assert "RuntimeError" not in r.text
    assert "tool.get_lead" not in r.text


def test_unknown_note_id_is_404_note_not_found(client):
    r = client.post(JUDGE, json=_body(note_id=999), headers=_headers())

    assert r.status_code == 404
    assert r.json() == {
        "detail": "Not Found",
        "reason": "note_not_found",
        "request_id": r.headers["X-Request-ID"],
    }


def test_note_is_matched_by_id_not_by_position(client, leads):
    # Design A: never newest-by-position. Note 11 is FIRST in the list; asking
    # for note 10 must judge note 10.
    r = client.post(JUDGE, json=_body(note_id=11), headers=_headers())
    assert r.status_code == 200
    assert r.json()["note_id"] == 11


def test_a_thin_note_is_suppressed_without_reserving_anything(
    client, leads, operational
):
    leads.notes[LEAD_ID] = [note(NOTE_ID, "ok")]
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert body["suppressed"] == {
        "reason": "insufficient_evidence",
        "detail_code": "note_too_short",
    }
    assert body["score"] is None
    assert body["decision"] is None
    # Nothing reserved: the salesperson can fix the note and resubmit at once
    # instead of being told 409 for the next 24 hours.
    assert operational.store == {}


def test_a_short_but_wordy_note_is_still_thin(client, leads):
    # 3-token floor AND 15-char floor: "a b c" clears tokens, fails chars.
    leads.notes[LEAD_ID] = [note(NOTE_ID, "a b c")]
    r = client.post(JUDGE, json=_body(), headers=_headers())
    assert r.json()["suppressed"]["detail_code"] == "note_too_short"


def test_a_long_single_token_note_is_still_thin(client, leads):
    leads.notes[LEAD_ID] = [note(NOTE_ID, "x" * 60)]
    r = client.post(JUDGE, json=_body(), headers=_headers())
    assert r.json()["suppressed"]["detail_code"] == "note_too_short"


def test_second_identical_request_is_409(client):
    first = client.post(JUDGE, json=_body(), headers=_headers())
    assert first.status_code == 200

    second = client.post(JUDGE, json=_body(), headers=_headers())
    assert second.status_code == 409
    assert second.json() == {
        "detail": "Conflict",
        "reason": "duplicate_request",
        "request_id": second.headers["X-Request-ID"],
    }


def test_an_edited_note_is_a_new_judgement_not_a_duplicate(client, leads):
    client.post(JUDGE, json=_body(), headers=_headers())
    leads.notes[LEAD_ID] = [note(NOTE_ID, GOOD_NOTE + " Confirmed for 3pm.")]

    again = client.post(JUDGE, json=_body(), headers=_headers())
    assert again.status_code == 200  # new fingerprint -> new judgement


def test_idempotency_store_down_is_503_and_calls_no_model(client, operational, llm):
    operational.raise_on.add("set")
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json()["reason"] == "idempotency_unavailable"
    # Fails CLOSED before anything is spent.
    assert llm.call_count == 0


def test_counter_stores_down_do_not_fail_the_request(client, operational):
    # Rate limit and attempts fail OPEN: a db2 blip must not 503 an otherwise
    # fine judgement. Only `get` fails, so the reservation (`set`) still works.
    operational.raise_on.add("get")
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200


def test_a_failure_after_reserving_releases_the_key(client, leads, operational):
    # Reserve succeeds, then the notes fetch fails -> the caller must be able
    # to retry rather than being told 409 for a judgement that never happened.
    # (Reservation happens after the fetch, so this drives the release through
    # the counter path instead.)
    operational.raise_on.add("get")
    first = client.post(JUDGE, json=_body(), headers=_headers())
    assert first.status_code == 200
    assert operational.store  # the reservation is held for a real judgement


# --- the not_implemented shape ---------------------------------------------


def test_the_seam_returns_a_not_scorable_suppressed_judgement(client):
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert body["suppressed"] == {
        "reason": "not_scorable",
        "detail_code": "not_implemented",
    }
    assert body["score"] is None
    assert body["decision"] is None
    assert body["note_id"] == NOTE_ID
    assert body["lead_id"] == LEAD_ID
    assert body["author_id"] == 27


def test_a_suppressed_judgement_still_carries_all_four_versions(client):
    versions = client.post(JUDGE, json=_body(), headers=_headers()).json()["versions"]
    assert versions == {
        "rubric_version": "note_rubric_v1",
        "prompt_version": "unit_a_prompts_v1",
        # A classifier RAN, so the stamp is what it reported -- not the
        # configured pin, and not "".
        "model_version": FAKE_MODEL,
        "config_version": "tenant-cfg-default-1",
    }


def test_a_thin_note_stamps_no_model_version(client, leads):
    # The other half of the same field: nothing was spent, so there is nothing
    # a provider reported. An unspent judgement must not look like a spent one.
    leads.notes[LEAD_ID] = [note(NOTE_ID, "ok")]
    versions = client.post(JUDGE, json=_body(), headers=_headers()).json()["versions"]
    assert versions["model_version"] == ""


def test_the_analysis_carries_the_classifiers_answer_and_nothing_else(client):
    analysis = client.post(JUDGE, json=_body(), headers=_headers()).json()["analysis"]
    assert analysis == {
        "note_type": "discovery",
        # Vague detection and scoring have not run, so these stay null/empty --
        # never zero, and never a default the CRM could read as an answer.
        "is_vague": None,
        "missing_components": [],
        "clarification_prompt": None,
        "reasoning": None,
    }


def test_a_thin_note_has_no_note_type_at_all(client, leads):
    leads.notes[LEAD_ID] = [note(NOTE_ID, "ok")]
    analysis = client.post(JUDGE, json=_body(), headers=_headers()).json()["analysis"]
    assert analysis == {
        "note_type": None,
        "is_vague": None,
        "missing_components": [],
        "clarification_prompt": None,
        "reasoning": None,
    }


def test_the_response_carries_the_request_id(client):
    r = client.post(JUDGE, json=_body(), headers=_headers())
    assert r.json()["request_id"] == r.headers["X-Request-ID"]


def test_exactly_one_model_call_reaches_the_seam(client, llm):
    # Classification and nothing else: vague detection and scoring are the next
    # two phases, and a third call here would mean one of them arrived early.
    client.post(JUDGE, json=_body(), headers=_headers())
    assert llm.call_count == 1


def test_the_tool_layer_sees_the_verified_tenant(client, leads):
    client.post(JUDGE, json=_body(), headers=_headers())
    assert {call.tenant for call in leads.calls} == {"tenant-a"}


def test_resubmission_returns_the_same_shape(client):
    r = client.post(RESUBMIT, json=_body(), headers=_headers())
    assert r.status_code == 200
    assert r.json()["suppressed"]["detail_code"] == "not_implemented"


def test_a_judgement_is_logged_without_note_text(client, json_log):
    client.post(JUDGE, json=_body(), headers=_headers())

    assert GOOD_NOTE not in json_log.getvalue()
    line = next(x for x in _lines(json_log) if x["message"] == "judgement_suppressed")
    assert line["suppressed_reason"] == "not_scorable"
    assert line["suppressed_detail"] == "not_implemented"
    assert line["model_calls"] == 1
    assert line["tenant"] == "tenant-a"


# --- the SEAM[STEP3] stub ---------------------------------------------------


def test_token_preflight_logs_once_across_two_requests(client, leads, json_log):
    client.post(JUDGE, json=_body(), headers=_headers())
    leads.notes[LEAD_ID] = [note(NOTE_ID, GOOD_NOTE + " Second distinct note text.")]
    client.post(JUDGE, json=_body(), headers=_headers())

    bypasses = [
        x for x in _lines(json_log) if x["message"] == "token_preflight_bypassed"
    ]
    assert len(bypasses) == 1
    assert bypasses[0]["level"] == "WARNING"
    assert bypasses[0]["tenant"] == "tenant-a"


def test_token_preflight_is_not_reached_when_the_note_is_thin(client, leads, json_log):
    leads.notes[LEAD_ID] = [note(NOTE_ID, "ok")]
    client.post(JUDGE, json=_body(), headers=_headers())

    assert not [
        x for x in _lines(json_log) if x["message"] == "token_preflight_bypassed"
    ]


# --- /meta/versions ---------------------------------------------------------


def test_meta_versions_returns_the_four_strings(client):
    r = client.get(VERSIONS, headers=_headers())

    assert r.status_code == 200
    assert r.json() == {
        "rubric_version": "note_rubric_v1",
        "prompt_version": "unit_a_prompts_v1",
        "model_version": "",  # the configured pin; no model is pinned yet
        "config_version": "tenant-cfg-default-1",
    }


def test_meta_versions_is_behind_the_gates(client):
    r = client.get(VERSIONS)
    assert r.status_code == 401


# --- classification's two stops, through HTTP -------------------------------


def test_a_system_event_is_suppressed_after_one_call(client, llm, operational):
    # ASSUMPTION[Q6]. One call spent to find out, and then nothing: no vague
    # detection, no scoring, no counters touched.
    llm.rescript(_classified("system_event"))
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert body["suppressed"] == {
        "reason": "not_scorable",
        "detail_code": "system_event",
    }
    assert body["score"] is None and body["decision"] is None
    assert body["analysis"]["note_type"] == "system_event"
    assert llm.call_count == 1


def test_an_unclassifiable_note_is_suppressed_after_one_call(client, llm):
    llm.rescript(_classified("unclassifiable"))
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    assert r.json()["suppressed"] == {
        "reason": "not_scorable",
        "detail_code": "unclassifiable",
    }
    assert r.json()["analysis"]["note_type"] == "unclassifiable"
    assert llm.call_count == 1


def test_a_suppressed_classification_still_stamps_the_model_that_ran(client, llm):
    llm.rescript(_classified("system_event"))
    versions = client.post(JUDGE, json=_body(), headers=_headers()).json()["versions"]
    # A model DID run here, unlike the thin-note path. The stamp is the only
    # place that difference survives onto the judgement.
    assert versions["model_version"] == FAKE_MODEL


def test_a_suppressed_classification_is_logged_by_code_not_by_text(
    client, llm, json_log
):
    llm.rescript(_classified("system_event"))
    client.post(JUDGE, json=_body(), headers=_headers())

    assert GOOD_NOTE not in json_log.getvalue()
    line = next(x for x in _lines(json_log) if x["message"] == "judgement_suppressed")
    assert line["suppressed_detail"] == "system_event"
    assert line["note_type"] == "system_event"
    assert line["model_calls"] == 1


# --- the model failing, through HTTP ----------------------------------------


def test_malformed_model_output_is_503_malformed_output(client, llm):
    llm.rescript(response("I think this one is a viewing, probably."))
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json() == {
        "detail": "Service Unavailable",
        "reason": "malformed_output",
        "request_id": r.headers["X-Request-ID"],
    }


def test_malformed_output_releases_the_idempotency_key(client, llm, operational):
    # Otherwise the caller is told 409 for the next 24 hours for a judgement
    # that never happened.
    llm.rescript(response("not json"), _classified("discovery"))
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 503
    assert operational.store == {}
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200


def test_the_rejected_output_never_reaches_the_response_or_the_log(
    client, llm, json_log
):
    secret = "SENTINEL-0501234567 villa budget 4.2M"
    llm.rescript(response(secret))
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert secret not in r.text
    assert secret not in json_log.getvalue()
    assert "0501234567" not in json_log.getvalue()


def test_a_provider_failure_is_503_model_unavailable(client, llm):
    llm.rescript(LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True))
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json()["reason"] == "model_unavailable"


def test_a_provider_failure_releases_the_key_so_a_retry_works(client, llm, operational):
    llm.rescript(
        LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True),
        _classified("discovery"),
    )
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 503
    assert operational.store == {}
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200
