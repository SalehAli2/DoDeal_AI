"""Register item 22: /metrics behind DODEAL_METRICS_ENABLED, outside the gates
and the cap, with bounded labels that never name a tenant, subject, note or lead."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core import inflight as core_inflight
from dodeal_ai.core import metrics
from dodeal_ai.core.breaker import operational_breaker
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import (
    LeadNotFoundError,
    ModelUnavailableError,
    NoteNotFoundError,
)
from dodeal_ai.core.inflight import InflightCounter
from dodeal_ai.core.llm import LLMErrorReason, LLMProviderError
from dodeal_ai.main import app, create_app
from dodeal_ai.tools.errors import BackendNotFound
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import JudgementDeps, judge_note
from dodeal_ai.units.structured_intelligence.schemas import JudgementRequest
from tests.helpers import breakers
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response, response
from tests.helpers.score_answers import score_payload

LEAD_ID = 1656
NOTE_ID = 10
TEXT = "Called the client, discussed the New Cairo 3BR, following up Tuesday."
FORBIDDEN = ("tenant", "subject", "note", "lead")


def _value(name: str, labels: dict[str, str] | None = None) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _happy() -> list:
    return [
        json_response({"note_type": "discovery"}),
        json_response(
            {
                "is_vague": False,
                "missing_components": [],
                "clarification_prompt": None,
                "reasoning": "Complete.",
            }
        ),
        json_response(score_payload()),
    ]


def _deps(llm: FakeLLM, leads: FakeLeadsClient | None = None) -> JudgementDeps:
    return JudgementDeps(
        leads=leads
        or FakeLeadsClient(
            leads={LEAD_ID: lead(LEAD_ID)}, notes={LEAD_ID: [note(NOTE_ID, TEXT)]}
        ),
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )


def _scope():
    return RequestContext(
        tenant="tenant-a",
        subject="42",
        database="crm_tenant_a",
        roles=(),
        permissions=frozenset(),
        request_id="req-1",
    ).scope()


async def _judge(deps: JudgementDeps, note_id: int = NOTE_ID):
    return await judge_note(
        _scope(),
        JudgementRequest(lead_id=LEAD_ID, note_id=note_id),
        resubmission=False,
        deps=deps,
    )


# --- the page -------------------------------------------------------------------


def test_the_page_is_404_unless_enabled():
    """Off by default: the page does not exist until DODEAL_METRICS_ENABLED."""
    assert get_settings().metrics_enabled is False
    assert TestClient(app).get("/metrics").status_code == 404


def test_the_page_needs_no_token_and_no_tenant_host(monkeypatch):
    """Enabled, it answers without Authorization and on any Host: no gate runs."""
    monkeypatch.setenv("DODEAL_METRICS_ENABLED", "true")
    get_settings.cache_clear()

    r = TestClient(app).get("/metrics")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    for name in (
        "judgements_total",
        "model_calls_total",
        "bypass_total",
        "load_shed_total",
        "backend_errors_total",
        "breaker_state",
        "judgement_seconds",
    ):
        assert f"# TYPE {name.removesuffix('_total')}" in r.text
    get_settings.cache_clear()


def test_the_page_is_outside_the_in_flight_cap(monkeypatch):
    """A full cap still serves /metrics, and a shed request is counted."""

    class _Full(InflightCounter):
        def acquire(self, limit: int) -> bool:
            return False

    monkeypatch.setattr(core_inflight, "_counter", _Full())
    monkeypatch.setenv("DODEAL_METRICS_ENABLED", "true")
    get_settings.cache_clear()
    before = _value("load_shed_total")

    client = TestClient(app)
    assert client.get("/metrics").status_code == 200
    assert client.post("/api/v1/notes/judgements", json={}).status_code == 503

    assert _value("load_shed_total") == before + 1
    get_settings.cache_clear()


def test_the_page_is_not_in_the_openapi_schema():
    """An operational page, not part of the API contract."""
    assert "/metrics" not in create_app().openapi()["paths"]


# --- the counters -------------------------------------------------------------


async def test_a_completed_judgement_counts_its_outcome_calls_and_time():
    """completed +1, three ok calls, one duration observed."""
    completed = _value("judgements_total", {"outcome": "completed"})
    ok = {
        p: _value("model_calls_total", {"pass": p, "outcome": "ok"})
        for p in ("classify", "vague", "score")
    }
    timed = _value("judgement_seconds_count")

    await _judge(_deps(FakeLLM(*_happy())))

    assert _value("judgements_total", {"outcome": "completed"}) == completed + 1
    for pass_name, count in ok.items():
        assert (
            _value("model_calls_total", {"pass": pass_name, "outcome": "ok"})
            == count + 1
        )
    assert _value("judgement_seconds_count") == timed + 1


async def test_a_replay_a_failure_and_a_suppression_are_counted_by_outcome():
    """replayed, note_not_found and suppressed each have their own series."""
    deps = _deps(FakeLLM(*_happy()))
    replayed = _value("judgements_total", {"outcome": "replayed"})
    missing = _value("judgements_total", {"outcome": "note_not_found"})
    suppressed = _value("judgements_total", {"outcome": "suppressed"})

    await _judge(deps)
    await _judge(deps)
    with pytest.raises(NoteNotFoundError):
        await _judge(deps, note_id=999)
    thin = FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)}, notes={LEAD_ID: [note(NOTE_ID, "ok")]}
    )
    await _judge(_deps(FakeLLM(), thin))

    assert _value("judgements_total", {"outcome": "replayed"}) == replayed + 1
    assert _value("judgements_total", {"outcome": "note_not_found"}) == missing + 1
    assert _value("judgements_total", {"outcome": "suppressed"}) == suppressed + 1


async def test_malformed_and_unavailable_calls_are_counted():
    """A reprompted pass counts malformed then ok; a dead provider counts unavailable."""
    malformed = _value(
        "model_calls_total", {"pass": "classify", "outcome": "malformed"}
    )
    unavailable = _value(
        "model_calls_total", {"pass": "classify", "outcome": "unavailable"}
    )

    await _judge(_deps(FakeLLM(response("not json"), *_happy())))
    # Another note, so the first judgement's stored answer is not replayed.
    other = FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)}, notes={LEAD_ID: [note(11, f"{TEXT} Again.")]}
    )
    dead = FakeLLM(LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True))
    with pytest.raises(ModelUnavailableError):
        await _judge(_deps(dead, other), note_id=11)

    assert (
        _value("model_calls_total", {"pass": "classify", "outcome": "malformed"})
        == malformed + 1
    )
    assert (
        _value("model_calls_total", {"pass": "classify", "outcome": "unavailable"})
        == unavailable + 1
    )


async def test_a_bypass_and_a_backend_error_are_counted(redis_fakes):
    """A dead attempt store counts its bypass event; a missing lead counts not_found."""
    bypassed = _value("bypass_total", {"event": "attempt_counter_bypassed"})
    not_found = _value("backend_errors_total", {"kind": "not_found"})
    redis_fakes.operational.raise_on.add("get")

    await _judge(_deps(FakeLLM(*_happy())))
    redis_fakes.operational.raise_on.clear()
    leads = FakeLeadsClient(
        raise_on={"get_lead": BackendNotFound("tool.get_lead", 404)}
    )
    with pytest.raises(LeadNotFoundError):
        await _judge(_deps(FakeLLM(), leads))

    assert _value("bypass_total", {"event": "attempt_counter_bypassed"}) >= bypassed + 1
    assert _value("backend_errors_total", {"kind": "not_found"}) == not_found + 1


async def test_the_breaker_gauge_reads_state_at_scrape_time():
    """A tripped operational breaker scrapes as 2, a fresh one as 0."""
    breaker = operational_breaker()
    assert _value("breaker_state", {"breaker": "operational"}) == 0
    await breakers.trip(breaker)
    assert _value("breaker_state", {"breaker": "operational"}) == 2


# --- the labels ---------------------------------------------------------------


async def test_no_label_names_a_tenant_subject_note_or_lead(redis_fakes):
    """Every declared and every emitted label name is free of identity words."""
    await _judge(_deps(FakeLLM(*_happy())))
    operational_breaker()

    declared = [
        name
        for metric in (
            metrics.JUDGEMENTS,
            metrics.MODEL_CALLS,
            metrics.BYPASSES,
            metrics.LOAD_SHED,
            metrics.BACKEND_ERRORS,
            metrics.JUDGEMENT_SECONDS,
        )
        for name in metric._labelnames
    ]
    emitted = [
        name
        for family in metrics.REGISTRY.collect()
        for sample in family.samples
        for name in sample.labels
    ]
    for name in declared + emitted:
        assert not any(word in name.lower() for word in FORBIDDEN), name
    assert set(declared) == {"outcome", "pass", "event", "kind"}
    assert "tenant-a" not in metrics.render().decode()
