"""The history route (register item 127): old notes scored for the measures'
past, never asking anyone anything, never touching a db2 counter, charged to
the tenant's history token budget alone, and bounded by its own bulkhead."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import dodeal_ai.core.cost.limiter as cost_limiter
from dodeal_ai.core import metrics
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.inflight import history_counter
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence import pipeline, state
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.fake_operational_redis import FakeOperationalRedis
from tests.helpers.score_answers import score_payload

HISTORY = "/api/v1/notes/judgements/history"
DIRECT = "/api/v1/notes/judgements/direct"
GOOD_NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."
WRITTEN = "2026-03-04T09:15:00+04:00"

VAGUE_ANSWER = {
    "is_vague": True,
    "missing_components": ["next_step_with_date"],
    "clarification_prompt": "Which Tuesday are you calling, and what will you cover?",
    "reasoning": "The follow-up has no date.",
}

# The db2 keys a live judgement moves and a history judgement must not.
_COUNTER_PREFIXES = ("attempt:", "attempt_fp:", "ratelimit:", "ratelimit_day:")


def _script(llm: FakeLLM, judgements: int = 1) -> None:
    llm.script_for(
        CLASSIFY_TEMPLATE,
        *[json_response({"note_type": NoteType.DISCOVERY.value})] * judgements,
    )
    llm.script_for(
        template_for(NoteType.DISCOVERY), *[json_response(VAGUE_ANSWER)] * judgements
    )
    llm.script_for(SCORE_TEMPLATE, *[json_response(score_payload())] * judgements)


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def operational() -> FakeOperationalRedis:
    return FakeOperationalRedis()


@pytest.fixture
def cost() -> FakeCostRedis:
    return FakeCostRedis()


@pytest.fixture
def client(monkeypatch, llm, operational, cost):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: cost)
    monkeypatch.setattr(state, "get_operational_client", lambda: operational)
    app.dependency_overrides[get_llm_client] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }


def _body(text: str = GOOD_NOTE, **overrides: object) -> dict:
    body: dict = {
        "lead_id": 1656,
        "note_id": 10,
        "author_id": 27,
        "note_text": text,
        "lead": {
            "leadType": "buyer",
            "enquiryType": "sale",
            "project": None,
            "status": None,
        },
        "note_created_at": WRITTEN,
    }
    body.update(overrides)
    return body


def _counter_keys(operational: FakeOperationalRedis) -> list[str]:
    return [key for key in operational.store if key.startswith(_COUNTER_PREFIXES)]


def _value(name: str, labels: dict[str, str]) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


# --- the judgement ----------------------------------------------------------


def test_a_vague_old_note_is_judged_and_never_asked_about(client, llm) -> None:
    """The score and the action are computed; the question is withheld history."""
    _script(llm)
    r = client.post(HISTORY, json=_body(), headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert body["score"]["band"] is not None
    assert body["decision"]["prompt_sent"] is False
    assert body["decision"]["prompt_withheld"] == "history"
    assert body["analysis"]["clarification_prompt"] is not None


def test_the_verdict_is_computed_as_for_a_live_note(client, llm) -> None:
    """A note a live judgement would flag is flagged here too, never blocked."""
    _script(llm)
    body = client.post(HISTORY, json=_body(), headers=_headers()).json()
    assert body["enforcement"]["verdict"] == "flag"
    assert body["enforcement"]["applies_to"] == "note"


def test_no_db2_counter_key_exists_after_a_history_judgement(
    client, llm, operational
) -> None:
    """No attempt, rate-limit or fingerprint key: only the history reservation."""
    _script(llm)
    client.post(HISTORY, json=_body(), headers=_headers())

    assert _counter_keys(operational) == []
    assert [key.split(":")[2] for key in operational.store] == ["judge_history"]


def test_only_the_history_token_key_moves(client, llm, cost) -> None:
    """Three paid calls, all charged to tokens:history:tenant, none to the live pair."""
    _script(llm)
    client.post(HISTORY, json=_body(), headers=_headers())

    token_keys = {key for key in cost.store if key.startswith("tokens:")}
    assert token_keys == {"tokens:history:tenant:tenant-a"}
    assert cost.mgets == [("tokens:history:tenant:tenant-a",)]


def test_a_history_judgements_outcome_lines_and_metrics_say_history(
    client, llm, caplog
) -> None:
    """The guard (register item 72): both outcome lines, and both metrics."""
    caplog.set_level(logging.INFO, logger="dodeal_ai")
    completed = _value("judgements_total", {"outcome": "completed", "route": "history"})
    thin = _value("judgements_total", {"outcome": "suppressed", "route": "history"})
    timed = _value("judgement_seconds_count", {"route": "history"})
    _script(llm)

    client.post(HISTORY, json=_body(), headers=_headers())
    client.post(HISTORY, json=_body("ok", note_id=11), headers=_headers())

    lines = {
        record.getMessage(): getattr(record, "route", None)
        for record in caplog.records
        if record.getMessage() in ("judgement_completed", "judgement_suppressed")
    }
    assert lines == {
        "judgement_completed": "history",
        "judgement_suppressed": "history",
    }
    labels = {"outcome": "completed", "route": "history"}
    assert _value("judgements_total", labels) == completed + 1
    labels = {"outcome": "suppressed", "route": "history"}
    assert _value("judgements_total", labels) == thin + 1
    assert _value("judgement_seconds_count", {"route": "history"}) == timed + 2


def test_a_thin_old_note_withholds_its_fixed_question_as_history(
    client, llm, operational
) -> None:
    """The fixed question is computed and withheld, and no slot is taken."""
    r = client.post(HISTORY, json=_body("ok"), headers=_headers())

    assert r.status_code == 200
    suppressed = r.json()["suppressed"]
    assert suppressed["detail_code"] == "note_too_short"
    assert suppressed["prompt_withheld"] == "history"
    assert suppressed["clarification_prompt"] is not None
    assert operational.store == {}
    assert llm.call_count == 0


def test_the_notes_own_date_is_its_created_at(client, llm, monkeypatch) -> None:
    """note_created_at, not the arrival time, is the LeadNote's createdAt."""
    seen: list[datetime] = []
    real = pipeline._judge

    async def spy(scope, request, lead, note, **kwargs):
        seen.append(note.createdAt)
        return await real(scope, request, lead, note, **kwargs)

    monkeypatch.setattr(pipeline, "_judge", spy)
    _script(llm)
    client.post(HISTORY, json=_body(), headers=_headers())

    assert seen == [datetime.fromisoformat(WRITTEN)]
    assert seen[0].astimezone(UTC).hour == 5


def test_a_repeat_is_a_replay_and_a_live_judgement_is_not(client, llm) -> None:
    """History has its own idempotency namespace: live and history never cross."""
    _script(llm, judgements=2)
    direct = {k: v for k, v in _body().items() if k != "note_created_at"}

    assert client.post(DIRECT, json=direct, headers=_headers()).status_code == 200
    first = client.post(HISTORY, json=_body(), headers=_headers())
    again = client.post(HISTORY, json=_body(), headers=_headers())

    assert "Idempotent-Replay" not in first.headers
    assert first.json()["decision"]["prompt_withheld"] == "history"
    assert again.headers["Idempotent-Replay"] == "true"
    assert llm.call_count == 6


# --- the body and the credential --------------------------------------------


@pytest.mark.parametrize("created_at", ["2026-03-04T09:15:00", "not a date", None])
def test_a_naive_missing_or_bad_date_is_422(client, llm, created_at) -> None:
    """A time with no offset is refused rather than guessed at."""
    body = _body()
    if created_at is None:
        del body["note_created_at"]
    else:
        body["note_created_at"] = created_at
    r = client.post(HISTORY, json=body, headers=_headers())
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid_request"
    assert llm.call_count == 0


def test_an_extra_field_is_422(client) -> None:
    """extra=forbid, as on the direct body."""
    r = client.post(HISTORY, json=_body(band="good"), headers=_headers())
    assert r.status_code == 422


def test_a_user_token_is_401(client, llm) -> None:
    """The service chain only."""
    user = {
        "Authorization": f"Bearer {tokens.mint_token(subdomain='tenant-a', sub=42)}",
        "Host": "tenant-a.dodealcrm.com",
    }
    assert client.post(HISTORY, json=_body(), headers=user).status_code == 401
    assert llm.call_count == 0


# --- the budget -------------------------------------------------------------


def test_the_history_budget_is_its_own(client, llm, cost, monkeypatch) -> None:
    """A spent live budget does not stop history; a spent history budget does."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "10")
    monkeypatch.setenv("DODEAL_COST_TOKENS_HISTORY_PER_TENANT_LIMIT", "10")
    get_settings.cache_clear()
    cost.store["tokens:tenant:tenant-a"] = 10
    _script(llm)
    assert client.post(HISTORY, json=_body(), headers=_headers()).status_code == 200

    cost.store["tokens:history:tenant:tenant-a"] = 10
    r = client.post(HISTORY, json=_body(note_id=11), headers=_headers())
    assert r.status_code == 429
    assert r.json()["reason"] == "token_budget_exceeded"


def test_ten_history_requests_leave_the_live_request_counter_untouched(
    client, llm, cost
) -> None:
    """The guard (register item 127): history counts on its own request key."""
    _script(llm, judgements=10)
    for note_id in range(10, 20):
        r = client.post(HISTORY, json=_body(note_id=note_id), headers=_headers())
        assert r.status_code == 200

    assert "cost:tenant:tenant-a" not in cost.store
    assert cost.store["cost:history:tenant:tenant-a"] == 10


def test_a_spent_history_request_cap_is_429_and_live_notes_still_pass(
    client, llm, monkeypatch
) -> None:
    """Over its own cap history is refused at Gate 4; the live route is not."""
    monkeypatch.setenv("DODEAL_COST_HISTORY_PER_TENANT_LIMIT", "1")
    get_settings.cache_clear()
    _script(llm, judgements=2)
    assert client.post(HISTORY, json=_body(), headers=_headers()).status_code == 200

    refused = client.post(HISTORY, json=_body(note_id=11), headers=_headers())
    direct = {k: v for k, v in _body(note_id=12).items() if k != "note_created_at"}
    live = client.post(DIRECT, json=direct, headers=_headers())

    assert (refused.status_code, refused.json()) == (
        429,
        {"detail": "Too Many Requests"},
    )
    assert live.status_code == 200


# --- the bulkhead -----------------------------------------------------------


def test_with_eight_in_flight_the_ninth_history_is_503_but_a_live_note_passes(
    client, llm
) -> None:
    """The bulkhead refuses history alone, with Retry-After, before any spend."""
    _script(llm)
    held = 0
    try:
        while history_counter.acquire(get_settings().history_max_inflight):
            held += 1
        assert held == 8

        refused = client.post(HISTORY, json=_body(), headers=_headers())
        direct = {k: v for k, v in _body().items() if k != "note_created_at"}
        live = client.post(DIRECT, json=direct, headers=_headers())
    finally:
        for _ in range(held):
            history_counter.release()

    assert refused.status_code == 503
    assert refused.json()["reason"] == "history_load_shed"
    assert refused.headers["Retry-After"] == "1"
    assert live.status_code == 200
    assert llm.call_count == 3


def test_the_slot_is_given_back_after_success_and_failure(client, llm) -> None:
    """A finished or failed history judgement leaves the bulkhead empty."""
    _script(llm)
    client.post(HISTORY, json=_body(), headers=_headers())
    assert history_counter.count == 0

    app.dependency_overrides[get_llm_client] = lambda: FakeLLM(
        RuntimeError("provider exploded")
    )
    failed = TestClient(app, raise_server_exceptions=False).post(
        HISTORY, json=_body(note_id=12), headers=_headers()
    )
    assert failed.status_code in (500, 503)
    assert history_counter.count == 0
