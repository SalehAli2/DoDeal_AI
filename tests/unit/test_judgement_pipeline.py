"""judge_note called directly, for the contracts the routes cannot yet reach.

The release-on-failure path is the important one. Nothing after the
reservation raises in the current stub -- the two counter reads fail open and
the token pre-flight is a no-op -- so through HTTP this path is unreachable
today. It becomes the difference between "retry your request" and "409 for the
next 24 hours" the moment the model calls land in the next phase, so it is
tested now, at the seam, rather than after something starts depending on it.
"""

from __future__ import annotations

import pytest

import dodeal_ai.units.structured_intelligence.pipeline as pipeline_module
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import (
    BackendUnavailableError,
    DuplicateRequestError,
    IdempotencyUnavailableResponse,
    NoteNotFoundError,
)
from dodeal_ai.core.llm import LLMErrorReason, LLMProviderError
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.tools.keys import BackendKeyError
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import JudgementDeps, judge_note
from dodeal_ai.units.structured_intelligence.schemas import JudgementRequest
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM
from tests.helpers.fake_operational_redis import FakeOperationalRedis

LEAD_ID = 1656
NOTE_ID = 10
GOOD_NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."


def _scope(tenant: str = "tenant-a"):
    return RequestContext(
        tenant=tenant,
        subject="42",
        database="crm_tenant_a",
        roles=(),
        permissions=frozenset(),
        request_id="req-1",
    ).scope()


@pytest.fixture
def leads() -> FakeLeadsClient:
    return FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)},
        notes={LEAD_ID: [note(NOTE_ID, GOOD_NOTE)]},
    )


@pytest.fixture
def operational(monkeypatch) -> FakeOperationalRedis:
    client = FakeOperationalRedis()
    monkeypatch.setattr(state, "get_operational_client", lambda: client)
    return client


@pytest.fixture
def deps(leads: FakeLeadsClient) -> JudgementDeps:
    return JudgementDeps(
        leads=leads, llm=FakeLLM(), config=get_tenant_config("tenant-a")
    )


def _request(lead_id: int = LEAD_ID, note_id: int = NOTE_ID) -> JudgementRequest:
    return JudgementRequest(lead_id=lead_id, note_id=note_id)


# --- release on every non-200 after reserving ------------------------------


async def test_a_failure_after_reserving_releases_the_reservation(
    monkeypatch, deps, operational
):
    async def _boom(scope):
        raise LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True)

    monkeypatch.setattr(pipeline_module, "token_preflight", _boom)

    with pytest.raises(LLMProviderError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    # The reservation is gone, so the caller can retry.
    assert operational.store == {}


async def test_after_a_release_the_same_request_succeeds(
    monkeypatch, deps, operational
):
    calls = {"n": 0}

    async def _boom_once(scope):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    monkeypatch.setattr(pipeline_module, "token_preflight", _boom_once)

    with pytest.raises(RuntimeError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    # Without the release this would be 409 for the next 24 hours.
    judgement = await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    assert judgement.suppressed is not None


async def test_the_original_failure_is_not_replaced_by_the_release(
    monkeypatch, deps, operational
):
    async def _boom(scope):
        raise RuntimeError("the real problem")

    monkeypatch.setattr(pipeline_module, "token_preflight", _boom)
    operational.raise_on.add("delete")  # the release itself also fails

    # The caller must still see the REAL failure, not a release error.
    with pytest.raises(RuntimeError, match="the real problem"):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)


# --- the stop points, straight through -------------------------------------


async def test_a_duplicate_is_refused(deps, operational):
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    with pytest.raises(DuplicateRequestError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)


async def test_a_missing_note_is_note_not_found(deps, operational):
    with pytest.raises(NoteNotFoundError):
        await judge_note(_scope(), _request(note_id=999), resubmission=False, deps=deps)


async def test_a_missing_note_reserves_nothing(deps, operational):
    with pytest.raises(NoteNotFoundError):
        await judge_note(_scope(), _request(note_id=999), resubmission=False, deps=deps)
    assert operational.store == {}


async def test_an_idempotency_outage_is_its_own_code(deps, operational):
    operational.raise_on.add("set")
    with pytest.raises(IdempotencyUnavailableResponse):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)


async def test_a_backend_key_failure_is_backend_unavailable(deps, leads, operational):
    # BackendKeyError is a CONFIGURATION fault, never retried and never wrapped
    # by the watchdog -- but from the caller's side it is still "we could not
    # reach the backend", and the real reason is already in the log.
    leads.raise_on["get_lead"] = BackendKeyError("tenant-a")
    with pytest.raises(BackendUnavailableError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)


async def test_a_notes_fetch_failure_is_backend_unavailable(deps, leads, operational):
    leads.raise_on["get_lead_notes"] = ExternalCallError(
        "tool.get_lead_notes", RuntimeError()
    )
    with pytest.raises(BackendUnavailableError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)


# --- the tool layer sees a scope, not a context ----------------------------


async def test_the_scope_reaches_the_tool_layer_unchanged(deps, leads, operational):
    await judge_note(_scope("tenant-b"), _request(), resubmission=False, deps=deps)
    assert [call.tenant for call in leads.calls] == ["tenant-b", "tenant-b"]


async def test_both_fetches_are_made_for_the_requested_lead(deps, leads, operational):
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    assert [(c.method, c.lead_id) for c in leads.calls] == [
        ("get_lead", LEAD_ID),
        ("get_lead_notes", LEAD_ID),
    ]


async def test_no_model_call_happens_before_the_seam(deps, operational):
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    assert deps.llm.call_count == 0
