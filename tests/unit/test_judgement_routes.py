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
from dodeal_ai.tools.errors import BackendNotFound, BackendRejected
from dodeal_ai.tools.leads import get_leads_client
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import (
    CLASSIFY_MAX_OUTPUT_TOKENS,
    CLASSIFY_TEMPLATE,
)
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import (
    SCORE_MAX_OUTPUT_TOKENS,
    SCORE_TEMPLATE,
)
from dodeal_ai.units.structured_intelligence.vague import (
    VAGUE_MAX_OUTPUT_TOKENS,
    template_for,
)
from tests.helpers import tokens
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import (
    FAKE_MODEL,
    FakeLLM,
    json_response,
    response,
    stable_for,
    truncated,
)
from tests.helpers.fake_operational_redis import FakeOperationalRedis
from tests.helpers.score_answers import score_payload

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


def _vague_answer(
    *,
    is_vague: bool = True,
    missing: list[str] | None = None,
    prompt: str | None = None,
):
    """The vague pass's answer. Vague by default, with the one missing thing the
    GOOD_NOTE above actually lacks a firm version of."""
    return json_response(
        {
            "is_vague": is_vague,
            "missing_components": ["next_step_with_date"]
            if missing is None
            else missing,
            "clarification_prompt": (
                "Which Tuesday are you calling, and what will you cover?"
                if prompt is None and is_vague
                else prompt
            ),
            "reasoning": "The follow-up has no date.",
        }
    )


# 18 of 80 -> 23, `poor`: only wh_outcome and cl_readable came back true.
_POOR = {
    "wh_action": False,
    "cs_present": False,
    "cs_own_terms": False,
    "cl_substance": False,
}


def _score_answer(**checks: bool):
    """The scoring pass's answer. The default checks give 55 of a denominator of
    80 -- 69, `fair` -- which is one mark below the accept threshold and so the
    most interesting default to carry into Phase H. Keyword overrides flip
    individual checks (tests/helpers/score_answers.py)."""
    return json_response(score_payload(**checks))


def _happy_path(note_type: str = "discovery"):
    """One judgement's three answers, in the order the pipeline issues them:
    classification first (it chooses the other two prompts), then vague detection
    and scoring, which are issued together."""
    return [_classified(note_type), _vague_answer(), _score_answer()]


@pytest.fixture
def leads() -> FakeLeadsClient:
    return FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)},
        notes={LEAD_ID: [note(NOTE_ID, GOOD_NOTE), note(11, "Left voicemail again.")]},
    )


@pytest.fixture
def llm() -> FakeLLM:
    """Two full judgements' worth -- enough for the tests that judge twice.

    Deliberately not an endless supply: an exhausted FakeLLM raises rather than
    returning a default, so a path that calls the model more often than the
    pipeline is specified to is loud instead of silently absorbed.
    """
    return FakeLLM(*(_happy_path() + _happy_path()))


@pytest.fixture
def operational() -> FakeOperationalRedis:
    return FakeOperationalRedis()


@pytest.fixture
def cost() -> FakeCostRedis:
    """db1. Returned rather than built inline so a test can read back which
    keys the pre-flight and the charge actually touched."""
    return FakeCostRedis()


@pytest.fixture
def client(monkeypatch, leads, llm, operational, cost):
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

    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: cost)
    monkeypatch.setattr(state, "get_operational_client", lambda: operational)

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


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("field", ["lead_id", "note_id"])
@pytest.mark.parametrize("path", [JUDGE, RESUBMIT])
def test_an_id_below_one_on_the_fetch_routes_is_422_without_echo(
    client, leads, llm, path, field, value
):
    """An id of 0 or -1 is a 422 whose body carries neither the field name nor the value, and nothing is fetched."""
    r = client.post(path, json={**_body(), field: value}, headers=_headers())

    assert r.status_code == 422
    request_id = r.headers["X-Request-ID"]
    assert r.json() == {
        "detail": "Unprocessable Entity",
        "reason": "invalid_request",
        "request_id": request_id,
    }
    assert field not in r.text
    assert str(value) not in r.text.replace(request_id, "")
    assert leads.calls == []
    assert llm.call_count == 0


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


def test_a_backend_outage_on_the_lead_is_backend_unavailable(client, leads):
    """A lead read that outlived its retry is 503 backend_unavailable, never 404."""
    leads.raise_on["get_lead"] = ExternalCallError("tool.get_lead", RuntimeError())
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json()["reason"] == "backend_unavailable"


def test_a_missing_lead_is_404_lead_not_found(client, leads):
    """Register item 89: the lead's own 404 reaches the CRM as lead_not_found."""
    leads.raise_on["get_lead"] = BackendNotFound("tool.get_lead", 404)
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 404
    assert r.json()["reason"] == "lead_not_found"


def test_a_rejected_read_is_503_backend_rejected(client, leads):
    """Any other 4xx from the CRM is 503 backend_rejected."""
    leads.raise_on["get_lead_notes"] = BackendRejected("tool.get_lead_notes", 400)
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json()["reason"] == "backend_rejected"


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
    client, leads, operational, llm
):
    leads.notes[LEAD_ID] = [note(NOTE_ID, "ok")]
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert body["suppressed"] == {
        "reason": "insufficient_evidence",
        "detail_code": "note_too_short",
        # Register item 64: a fixed question, no model, still capped like one.
        "clarification_prompt": (
            "What happened, what did the client say, and what is the next "
            "step with a date?"
        ),
        "prompt_withheld": None,
    }
    assert body["score"] is None
    assert body["decision"] is None
    # No JUDGEMENT is reserved: the salesperson can fix the note and resubmit
    # at once instead of being told 409 for the next 24 hours. The fixed
    # question's own attempt/rate slots are a separate store, and do move.
    assert not any(key.startswith("idem:") for key in operational.store)
    # And nothing spent: the check is before the reservation and before the
    # first thing that costs money.
    assert llm.call_count == 0


def test_a_short_but_wordy_note_is_still_thin(client, leads):
    # 3-token floor AND 15-char floor: "a b c" clears tokens, fails chars.
    leads.notes[LEAD_ID] = [note(NOTE_ID, "a b c")]
    r = client.post(JUDGE, json=_body(), headers=_headers())
    assert r.json()["suppressed"]["detail_code"] == "note_too_short"


def test_a_long_single_token_note_is_still_thin(client, leads):
    leads.notes[LEAD_ID] = [note(NOTE_ID, "x" * 60)]
    r = client.post(JUDGE, json=_body(), headers=_headers())
    assert r.json()["suppressed"]["detail_code"] == "note_too_short"


def test_second_identical_request_is_a_replay(client, llm):
    """Items 1 and 2: 200, the same body with this request's id, the replay header."""
    first = client.post(JUDGE, json=_body(), headers=_headers())
    assert first.status_code == 200
    assert "Idempotent-Replay" not in first.headers

    second = client.post(JUDGE, json=_body(), headers=_headers())
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json() == {
        **first.json(),
        "request_id": second.headers["X-Request-ID"],
    }
    assert second.json()["request_id"] != first.json()["request_id"]
    assert llm.call_count == 3


def test_a_request_meeting_an_in_flight_reservation_is_409(client, operational, llm):
    """A key that is still only reserved is a 409, with no model call."""
    key = state._idempotency_key("tenant-a", NOTE_ID, state.note_fingerprint(GOOD_NOTE))
    operational.store[key] = state._RESERVED

    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 409
    assert r.json() == {
        "detail": "Conflict",
        "reason": "duplicate_request",
        "request_id": r.headers["X-Request-ID"],
    }
    assert llm.call_count == 0


def test_an_invalid_stored_value_is_judged_again_over_http(
    client, operational, llm, json_log
):
    """A stored value that does not validate is re-judged, and never logged."""
    key = state._idempotency_key("tenant-a", NOTE_ID, state.note_fingerprint(GOOD_NOTE))
    operational.store[key] = '{"clarification_prompt": "SENTINEL-replay-3b9a"}'

    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    assert "Idempotent-Replay" not in r.headers
    assert llm.call_count == 3
    assert "SENTINEL-replay-3b9a" not in json_log.getvalue()
    assert [x for x in _lines(json_log) if x["message"] == "idempotency_replay_invalid"]


def test_the_resubmission_route_replays_too(client, llm):
    """The replay header is set on every judgement route that can replay."""
    first = client.post(RESUBMIT, json=_body(), headers=_headers())
    second = client.post(RESUBMIT, json=_body(), headers=_headers())
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.headers["Idempotent-Replay"] == "true"
    assert llm.call_count == 3


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


# --- the judged shape ------------------------------------------------------


def test_a_judgement_carries_all_four_versions(client):
    versions = client.post(JUDGE, json=_body(), headers=_headers()).json()["versions"]
    assert versions == {
        "rubric_version": "note_rubric_v2",
        "prompt_version": "unit_a_prompts_v3",
        # A classifier RAN, so the stamp is what it reported -- not the
        # configured pin, and not "".
        "model_version": FAKE_MODEL,
        "config_version": "tenant-cfg-default-4",
    }


def test_a_thin_note_stamps_no_model_version(client, leads):
    # The other half of the same field: nothing was spent, so there is nothing
    # a provider reported. An unspent judgement must not look like a spent one.
    leads.notes[LEAD_ID] = [note(NOTE_ID, "ok")]
    versions = client.post(JUDGE, json=_body(), headers=_headers()).json()["versions"]
    assert versions["model_version"] == ""


def test_the_analysis_carries_all_three_passes(client):
    analysis = client.post(JUDGE, json=_body(), headers=_headers()).json()["analysis"]
    assert analysis == {
        "note_type": "discovery",
        "is_vague": True,
        "missing_components": ["next_step_with_date"],
        "clarification_prompt": "Which Tuesday are you calling, and what will you cover?",
        "reasoning": "The follow-up has no date.",
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


def test_three_model_calls_on_the_happy_path(client, llm):
    # Classification, vague detection, scoring. A fourth would mean one of them
    # reprompted -- which only a malformed answer buys, and none of these is.
    client.post(JUDGE, json=_body(), headers=_headers())
    assert llm.call_count == 3


def test_the_calls_are_issued_in_the_one_order_they_may_be(client, llm):
    # Classification FIRST, because it chooses the other two prompts. The other
    # two are issued together, in the order gather was given them -- which is
    # what makes an ordered script a safe way to write these tests.
    client.post(JUDGE, json=_body(), headers=_headers())
    stables = [p.stable for p in llm.prompts]
    assert stables[0] == stable_for(CLASSIFY_TEMPLATE)
    assert stables[1] == stable_for(template_for(NoteType.DISCOVERY))
    assert stables[2] == stable_for(SCORE_TEMPLATE)


def test_the_tool_layer_sees_the_verified_tenant(client, leads):
    client.post(JUDGE, json=_body(), headers=_headers())
    assert {call.tenant for call in leads.calls} == {"tenant-a"}


def test_resubmission_returns_the_same_shape(client):
    r = client.post(RESUBMIT, json=_body(), headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "note_id",
        "lead_id",
        "author_id",
        "analysis",
        "score",
        "decision",
        "suppressed",
        "enforcement",
        "versions",
        "request_id",
    }
    # Same body, same shape. The difference is policy, not structure: nothing
    # is ever asked on this route.
    assert body["decision"]["prompt_sent"] is False
    assert body["decision"]["prompt_withheld"] == "resubmission"


def test_a_judgement_is_logged_without_note_text(client, json_log):
    client.post(JUDGE, json=_body(), headers=_headers())

    assert GOOD_NOTE not in json_log.getvalue()
    line = next(x for x in _lines(json_log) if x["message"] == "judgement_completed")
    assert line["band"] == "fair"  # 55 of 80 -> 69
    assert line["denominator"] == 80  # ASSUMPTION[Q13]: deal_specifics is out
    assert line["action"] == "accept_flag_prompt"
    assert line["prompt_sent"] is True
    assert line["prompt_withheld"] is None
    assert line["attempt"] == 1
    assert line["model_passes"] == 3
    assert line["tenant"] == "tenant-a"
    # The question we asked is not on the line either -- it is model output.
    assert "Which Tuesday" not in json_log.getvalue()


# --- the token pre-flight, through the route --------------------------------


def test_the_preflight_reads_the_token_keys_on_every_judgement(client, cost):
    """The pre-flight is a real read now, and it is the token keys it reads."""
    client.post(JUDGE, json=_body(), headers=_headers())

    assert cost.mgets == [("tokens:tenant:tenant-a", "tokens:user:tenant-a:42")]


def test_the_preflight_is_not_reached_when_the_note_is_thin(client, leads, cost):
    """A note too thin to judge costs no reservation and no pre-flight read."""
    leads.notes[LEAD_ID] = [note(NOTE_ID, "ok")]
    client.post(JUDGE, json=_body(), headers=_headers())

    assert cost.mgets == []


# --- /meta/versions ---------------------------------------------------------


def test_meta_versions_returns_the_four_strings(client):
    r = client.get(VERSIONS, headers=_headers())

    assert r.status_code == 200
    assert r.json() == {
        "rubric_version": "note_rubric_v2",
        "prompt_version": "unit_a_prompts_v3",
        "model_version": "",  # the configured pin; no model is pinned yet
        "config_version": "tenant-cfg-default-4",
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
        "clarification_prompt": None,
        "prompt_withheld": None,
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
        "clarification_prompt": None,
        "prompt_withheld": None,
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
    assert line["model_passes"] == 1


# --- the model failing, through HTTP ----------------------------------------


def test_malformed_model_output_is_503_malformed_output(client, llm):
    prose = response("I think this one is a viewing, probably.")
    llm.rescript(prose, prose)
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json() == {
        "detail": "Service Unavailable",
        "reason": "malformed_output",
        "request_id": r.headers["X-Request-ID"],
    }
    assert llm.call_count == 2


def test_malformed_output_releases_the_idempotency_key(client, llm, operational):
    # Otherwise the caller is told 409 for the next 24 hours for a judgement
    # that never happened.
    llm.rescript(response("not json"), response("still not json"), *_happy_path())
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 503
    assert operational.store == {}
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200


def test_the_rejected_output_never_reaches_the_response_or_the_log(
    client, llm, json_log
):
    secret = "SENTINEL-0501234567 villa budget 4.2M"
    llm.rescript(response(secret), response(secret))
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert secret not in r.text
    assert secret not in json_log.getvalue()
    assert "0501234567" not in json_log.getvalue()


# --- the reprompt, through the whole pipeline -------------------------------


def test_a_reprompt_on_one_pass_does_not_re_issue_the_other(client, llm, json_log):
    # Register item 14 meets Phase G. Vague detection and scoring are issued
    # together, and each enters call_model separately -- so a malformed vague
    # answer buys VAGUE a second call and leaves scoring alone. Four calls, not
    # five, and only one of them carries a tail.
    llm.rescript(
        _classified("discovery"),
        response("The note looks a bit thin to me."),
        _vague_answer(),
        _score_answer(),
    )
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    assert llm.call_count == 4

    classify_p, vague_p, vague_again, score_p = llm.prompts
    assert classify_p.stable == stable_for(CLASSIFY_TEMPLATE)
    assert vague_p.stable == stable_for(template_for(NoteType.DISCOVERY))
    assert score_p.stable == stable_for(SCORE_TEMPLATE)

    # The reprompt is vague detection's, and it is the same prompt plus a tail.
    assert vague_again.stable == vague_p.stable
    assert vague_again.variable == vague_p.variable
    assert vague_again.tail and not vague_p.tail
    # Scoring answered first time and was never re-issued.
    assert not score_p.tail

    reprompts = [x for x in _lines(json_log) if x["message"] == "reprompt_issued"]
    assert [x["label"] for x in reprompts] == ["llm.unit_a.vague"]


def test_a_persistently_malformed_pass_is_503_and_releases_the_key(
    client, llm, operational
):
    # Two calls on that pass, then it stops; the key is released so the caller
    # can retry rather than being told 409 for a judgement that never happened.
    llm.rescript(response("not json"), response("still not json"), *_happy_path())
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json()["reason"] == "malformed_output"
    assert llm.call_count == 2
    assert operational.store == {}

    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200


def test_each_pass_sends_its_own_output_ceiling(client, llm):
    # Register item 15: three passes, three ceilings, none of them the seam's
    # default. Vague detection has the largest because it is the only answer
    # that comes back in the language of the note.
    client.post(JUDGE, json=_body(), headers=_headers())

    assert [c.max_output_tokens for c in llm.calls] == [
        CLASSIFY_MAX_OUTPUT_TOKENS,
        VAGUE_MAX_OUTPUT_TOKENS,
        SCORE_MAX_OUTPUT_TOKENS,
    ]
    assert None not in [c.max_output_tokens for c in llm.calls]


def test_a_truncated_answer_is_reprompted_not_accepted(client, llm):
    # MAX_TOKENS is malformed even when the fragment parses. Without this the
    # judgement would be stamped on the beginning of an answer.
    llm.rescript(
        truncated(json.dumps({"note_type": "discovery"})),
        _classified("discovery"),
        _vague_answer(),
        _score_answer(),
    )
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    assert llm.call_count == 4


def test_a_provider_failure_is_503_model_unavailable(client, llm):
    llm.rescript(LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True))
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json()["reason"] == "model_unavailable"


def test_a_provider_failure_releases_the_key_so_a_retry_works(client, llm, operational):
    llm.rescript(
        LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True),
        *_happy_path(),
    )
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 503
    assert operational.store == {}
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200


# --- Phase H: the five full-flow scenarios ----------------------------------
#
# One request each, end to end: the gate chain, the fake CRM, the fake
# operational store and a scripted FakeLLM. What they assert that the unit
# tests cannot is the JOIN -- that the total the arithmetic produced reaches
# the decision the CRM is shown, and that the counters moved exactly when a
# question was actually asked.

RATE_KEY = "ratelimit:tenant-a:42"
ATTEMPT_KEY = f"attempt:tenant-a:{NOTE_ID}"
ATTEMPT_FP_KEY = f"attempt_fp:tenant-a:{NOTE_ID}"


def _idem_key(text: str = GOOD_NOTE) -> str:
    return f"idem:tenant-a:judge_note:{NOTE_ID}:{state.note_fingerprint(text)}"


def test_scenario_1_a_good_note_is_accepted_silently(client, llm, operational):
    # 63 of 80 -> 79, `good`, at or above accept_threshold. Nothing is asked,
    # because there is nothing wrong with the note -- and so nothing is counted.
    llm.rescript(
        _classified("discovery"),
        _vague_answer(is_vague=False, missing=[], prompt=None),
        _score_answer(ns_action=True, cl_substance=False),
    )
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert body["score"]["total"] == 79
    assert body["score"]["band"] == "good"
    assert body["suppressed"] is None
    assert body["decision"] == {
        "action": "accept_silent",
        "prompt_sent": False,
        # NOT nothing_to_ask: there was no prompt to withhold. A reason here
        # would tell the CRM we wanted to ask something and could not.
        "prompt_withheld": None,
        "attempt": 0,
        "attempts_remaining": 1,
        "original_note_fingerprint": None,
    }
    assert llm.call_count == 3
    assert RATE_KEY not in operational.store
    assert ATTEMPT_KEY not in operational.store


def test_scenario_2_a_fair_vague_note_is_flagged_and_the_prompt_is_sent(
    client, operational
):
    # The fixture's default answers: 55 of 80 -> 69, one mark below the accept
    # threshold, and vague with one thing missing. This is the case the whole
    # clarification loop exists for.
    body = client.post(JUDGE, json=_body(), headers=_headers()).json()

    assert body["score"]["total"] == 69
    assert body["score"]["band"] == "fair"
    assert body["score"]["denominator"] == 80  # ASSUMPTION[Q13]
    assert body["decision"] == {
        "action": "accept_flag_prompt",
        "prompt_sent": True,
        "prompt_withheld": None,
        "attempt": 1,
        "attempts_remaining": 0,
        "original_note_fingerprint": None,  # always null on the primary route
    }

    # Both counters moved, each with its own window.
    assert operational.store[RATE_KEY] == "1"
    assert operational.store[ATTEMPT_KEY] == "1"
    assert operational.ttls[RATE_KEY] == 3600
    assert operational.ttls[ATTEMPT_KEY] == 21600
    # And the reference a later resubmission will read back.
    assert operational.store[ATTEMPT_FP_KEY] == state.note_fingerprint(GOOD_NOTE)
    assert operational.ttls[ATTEMPT_FP_KEY] == operational.ttls[ATTEMPT_KEY]


def test_scenario_3_a_poor_note_at_the_rate_limit_is_advised_but_not_asked(
    client, llm, operational
):
    # rate_limit_per_hour is 3 and this subject is already at 3. The ADVICE is
    # unchanged -- the note is poor and the CRM is told so -- but we do not add
    # a fourth question to someone's day.
    operational.store[RATE_KEY] = "3"
    llm.rescript(
        _classified("discovery"),
        _vague_answer(),
        _score_answer(**_POOR),
    )
    body = client.post(JUDGE, json=_body(), headers=_headers()).json()

    assert body["score"]["total"] == 23  # 18 of 80
    assert body["score"]["band"] == "poor"
    assert body["decision"]["action"] == "prompt_clarification"
    assert body["decision"]["prompt_sent"] is False
    assert body["decision"]["prompt_withheld"] == "rate_limited"
    assert body["decision"]["attempt"] == 0
    # The question was still computed and is still in the response: the CRM
    # can show it. What it may not do is claim WE asked it.
    assert body["analysis"]["clarification_prompt"] is not None

    assert operational.store[RATE_KEY] == "3"  # not incremented
    assert ATTEMPT_KEY not in operational.store


def test_a_burst_of_four_sends_three_prompts_and_rate_limits_the_fourth(
    client, leads, llm, operational
):
    """Four notes from one subject send three prompts, and the store records three slots
    taken and one refused."""
    burst = [11, 12, 13, 14]
    leads.notes[LEAD_ID] = [note(n, f"{GOOD_NOTE} Ref {n}.") for n in burst]
    llm.rescript(*(_happy_path() * len(burst)))

    withheld = []
    for note_id in burst:
        body = client.post(
            JUDGE, json=_body(note_id=note_id), headers=_headers()
        ).json()
        withheld.append(body["decision"]["prompt_withheld"])

    assert withheld == [None, None, None, "rate_limited"]
    assert [outcome for _, _, outcome in operational.evals] == [
        *[state._SLOTS_ALLOWED] * 3,
        state._SLOTS_DENIED_BY_RATE,
    ]
    assert {keys[1] for _, keys, _ in operational.evals} == {RATE_KEY}
    # Three prompts sent, three counted: the refused fourth left it alone.
    assert operational.store[RATE_KEY] == "3"


def test_an_exhausted_window_outranks_having_nothing_to_ask(client, llm, operational):
    """With no question to ask, an exhausted window still reports rate_limited from a
    read alone."""
    operational.store[RATE_KEY] = "3"
    llm.rescript(
        _classified("discovery"),
        _vague_answer(is_vague=False, missing=[], prompt=None),
        _score_answer(**_POOR),
    )

    body = client.post(JUDGE, json=_body(), headers=_headers()).json()

    assert body["decision"]["prompt_withheld"] == "rate_limited"
    # A READ, not a take: nothing may be claimed for a prompt that does not
    # exist, so the script never runs.
    assert operational.evals == []
    assert operational.store[RATE_KEY] == "3"


def test_the_conditions_before_the_rate_limit_cost_no_db2_trip(
    client, leads, llm, operational
):
    """A resubmission and a note at its attempt cap are both answered without
    asking db2 about the rate limit at all."""
    operational.store[ATTEMPT_KEY] = "1"  # clarification_cap is 1

    capped = client.post(JUDGE, json=_body(), headers=_headers()).json()
    assert capped["decision"]["prompt_withheld"] == "attempt_cap"

    leads.notes[LEAD_ID] = [note(NOTE_ID, f"{GOOD_NOTE} Edited.")]
    resubmitted = client.post(RESUBMIT, json=_body(), headers=_headers()).json()
    assert resubmitted["decision"]["prompt_withheld"] == "resubmission"

    assert operational.evals == []
    assert RATE_KEY not in operational.store


def test_scenario_4_a_resubmission_is_a_new_judgement_that_asks_nothing(
    client, leads, operational
):
    first = client.post(JUDGE, json=_body(), headers=_headers())
    assert first.json()["decision"]["prompt_sent"] is True

    # The salesperson edited the note in answer to the question. Same note_id,
    # different text -- so a different fingerprint, and a new judgement.
    leads.notes[LEAD_ID] = [
        note(NOTE_ID, GOOD_NOTE + " Confirmed Tuesday 3pm at the New Cairo office.")
    ]
    again = client.post(RESUBMIT, json=_body(), headers=_headers())

    assert again.status_code == 200  # not 409
    decision = again.json()["decision"]
    assert decision["action"] == "accept_flag_prompt"
    assert decision["prompt_sent"] is False
    assert decision["prompt_withheld"] == "resubmission"
    assert decision["attempt"] == 1  # read, never incremented
    assert decision["attempts_remaining"] == 0
    # Register item 33: the note we FIRST prompted on, so the CRM can link this
    # judgement to the one it followed without us holding any history.
    assert decision["original_note_fingerprint"] == state.note_fingerprint(GOOD_NOTE)
    assert operational.store[ATTEMPT_KEY] == "1"


def test_scenario_5_an_identical_second_save_is_409_with_zero_model_calls(
    client, llm, operational
):
    # The reservation the first save left behind. Nothing is fetched twice and
    # nothing is paid for twice -- the count is asserted, not assumed.
    operational.store[_idem_key()] = "1"

    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 409
    assert r.json()["reason"] == "duplicate_request"
    assert llm.call_count == 0


# --- Phase H: the four edges ------------------------------------------------


def test_a_note_already_at_the_attempt_cap_withholds_for_that_reason(
    client, operational
):
    # clarification_cap is 1 and this note has already had its question. The
    # cap is about the NOTE; the rate limit is about the person -- and the cap
    # is checked first, because it is the more specific fact.
    operational.store[ATTEMPT_KEY] = "1"

    decision = client.post(JUDGE, json=_body(), headers=_headers()).json()["decision"]

    assert decision["action"] == "accept_flag_prompt"
    assert decision["prompt_sent"] is False
    assert decision["prompt_withheld"] == "attempt_cap"
    assert decision["attempt"] == 1
    assert decision["attempts_remaining"] == 0
    assert operational.store[ATTEMPT_KEY] == "1"  # unchanged
    assert RATE_KEY not in operational.store


def test_a_fair_but_not_vague_note_withholds_because_there_is_nothing_to_ask(
    client, llm
):
    # 69, so the action is still accept_flag_prompt -- but the vague pass found
    # nothing missing, so there is no question. "Nothing to ask" is a REASON,
    # not an absent field the CRM has to interpret.
    llm.rescript(
        _classified("discovery"),
        _vague_answer(is_vague=False, missing=[], prompt=None),
        _score_answer(),
    )
    body = client.post(JUDGE, json=_body(), headers=_headers()).json()

    assert body["score"]["band"] == "fair"
    assert body["decision"]["action"] == "accept_flag_prompt"
    assert body["decision"]["prompt_sent"] is False
    assert body["decision"]["prompt_withheld"] == "nothing_to_ask"
    assert body["analysis"]["clarification_prompt"] is None


def test_the_counter_store_being_down_bypasses_it_without_failing_the_request(
    client, operational, json_log
):
    # Every command the counters use is broken (the attempt read and the
    # prompt-slots script). They fail open: the prompt is sent, nothing is
    # counted, and a politeness guard being down never 503s the judgement.
    operational.raise_on.update({"get", "eval"})

    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 200
    assert r.json()["decision"]["prompt_sent"] is True
    codes = [x.get("reason_code") for x in _lines(json_log)]
    assert "rate_limit_bypassed" in codes
    assert "attempt_counter_bypassed" in codes


def test_a_model_failure_then_a_retry_of_the_same_note_is_a_full_judgement(
    client, llm, operational
):
    # The release path, at the route level: a 503 of OURS must not cost the
    # caller a 409 for the next 24 hours. And the failed attempt cost nothing
    # -- one prompt was sent in total, so the attempt counter reads 1, not 2.
    llm.rescript(
        LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True),
        *_happy_path(),
    )
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 503
    assert operational.store == {}

    again = client.post(JUDGE, json=_body(), headers=_headers())

    assert again.status_code == 200
    assert again.json()["decision"]["prompt_sent"] is True
    assert operational.store[ATTEMPT_KEY] == "1"


# --- the resubmission reference (register item 33) --------------------------


def test_a_resubmission_with_no_prior_prompt_carries_a_null_reference(client):
    # Nothing was ever asked about this note, so there is nothing to link to.
    decision = client.post(RESUBMIT, json=_body(), headers=_headers()).json()[
        "decision"
    ]
    assert decision["original_note_fingerprint"] is None


def test_the_reference_is_null_when_the_attempt_store_is_down(
    client, operational, json_log
):
    # Fails OPEN with the attempt counter's own code -- no new bypass code, and
    # no 503. A missing reference costs a null field, never a judgement.
    operational.store[ATTEMPT_FP_KEY] = state.note_fingerprint(GOOD_NOTE)
    operational.raise_on.add("get")

    body = client.post(RESUBMIT, json=_body(), headers=_headers()).json()

    assert body["decision"]["original_note_fingerprint"] is None
    codes = [x.get("reason_code") for x in _lines(json_log)]
    assert "attempt_counter_bypassed" in codes


def test_the_reference_is_a_fingerprint_and_never_the_note(client, operational):
    client.post(JUDGE, json=_body(), headers=_headers())

    stored = operational.store[ATTEMPT_FP_KEY]
    assert stored == state.note_fingerprint(GOOD_NOTE)
    assert GOOD_NOTE not in stored
    assert len(stored) == 64 and set(stored) <= set("0123456789abcdef")


# --- the version stamp is the pass the judgement came from ------------------


def test_the_stamp_is_the_model_that_scored_not_the_one_that_classified(
    client, llm, json_log
):
    # The marks ARE the judgement, so the stamp is the model that produced
    # them. A provider that rolled a model between the first call and the third
    # would otherwise have the judgement attributed to the wrong one.
    llm.rescript(
        _classified("discovery"),
        _vague_answer(),
        json_response(
            score_payload(),
            model="model-that-scored",
        ),
    )
    body = client.post(JUDGE, json=_body(), headers=_headers()).json()

    assert body["versions"]["model_version"] == "model-that-scored"

    # And the disagreement is observable rather than silent.
    line = next(x for x in _lines(json_log) if x["message"] == "model_version_mismatch")
    assert line["level"] == "WARNING"
    assert line["labels"] == "llm.unit_a.classify,llm.unit_a.vague,llm.unit_a.score"
    assert line["tenant"] == "tenant-a"
    # Labels, never content.
    assert GOOD_NOTE not in json_log.getvalue()


def test_three_passes_from_one_model_log_no_mismatch(client, json_log):
    client.post(JUDGE, json=_body(), headers=_headers())
    assert not [x for x in _lines(json_log) if x["message"] == "model_version_mismatch"]


# --- Phase G's claim, landed (§2.9) -----------------------------------------


def test_a_provider_failure_on_the_reprompt_is_model_unavailable_and_releases(
    client, llm, operational
):
    # The first answer was malformed, so the pass earned its one reprompt -- and
    # THAT call is the one the provider failed. The caller is told the model was
    # unavailable, which is what happened, not malformed_output, which is what
    # the first answer was.
    llm.rescript(
        response("not json"),
        LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True),
        *_happy_path(),
    )
    r = client.post(JUDGE, json=_body(), headers=_headers())

    assert r.status_code == 503
    assert r.json()["reason"] == "model_unavailable"
    assert llm.call_count == 2
    assert operational.store == {}  # released, so the retry below is not a 409

    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200
