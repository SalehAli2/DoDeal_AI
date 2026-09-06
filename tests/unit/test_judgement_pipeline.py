"""judge_note called directly, for the contracts the routes reach awkwardly.

The release-on-failure path is the important one. Through HTTP it is reachable
only through a model failure, which is one specific way of failing after the
reservation; here the failure is INJECTED at the token pre-flight, so the
release is exercised for "anything at all raised after we reserved" rather than
for the one cause the route tests happen to have. It is the difference between
"retry your request" and "409 for the next 24 hours".
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest

import dodeal_ai.units.structured_intelligence.pipeline as pipeline_module
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import (
    BackendUnavailableError,
    DuplicateRequestError,
    IdempotencyUnavailableResponse,
    ModelUnavailableError,
    NoteNotFoundError,
)
from dodeal_ai.core.llm import LLMErrorReason, LLMProviderError
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.tools.keys import BackendKeyError
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import JudgementDeps, judge_note
from dodeal_ai.units.structured_intelligence.schemas import (
    JudgementRequest,
    SuppressedDetail,
)
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.fake_operational_redis import FakeOperationalRedis

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


def _score_answer(**marks: int):
    """The scoring pass's answer. The default marks sum to 55 of a denominator of
    80 -- 69, `fair` -- which is one mark below the accept threshold and so the
    most interesting default to carry into Phase H."""
    return json_response(
        {
            "marks": {
                "what_happened": 20,
                "client_said": 15,
                "next_step_date": 15,
                "clarity": 5,
                **marks,
            }
        }
    )


def _happy_path(note_type: str = "discovery"):
    """One judgement's three answers, in the order the pipeline issues them:
    classification first (it chooses the other two prompts), then vague detection
    and scoring, which are issued together."""
    return [_classified(note_type), _vague_answer(), _score_answer()]


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
def llm() -> FakeLLM:
    return FakeLLM(*(_happy_path() + _happy_path()))


@pytest.fixture
def json_capture():
    """The real JsonFormatter over the dodeal_ai tree, returned as a callable
    yielding the parsed lines emitted so far."""
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


@pytest.fixture
def deps(leads: FakeLeadsClient, llm: FakeLLM) -> JudgementDeps:
    return JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
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
    assert judgement.suppressed is None
    assert judgement.score is not None and judgement.decision is not None


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


async def test_the_three_passes_are_called(deps, operational):
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    assert deps.llm.call_count == 3


async def test_a_stop_before_the_seam_spends_nothing(deps, operational):
    with pytest.raises(NoteNotFoundError):
        await judge_note(_scope(), _request(note_id=999), resubmission=False, deps=deps)
    assert deps.llm.call_count == 0


# --- vague and scoring run concurrently (register item 14) ------------------


async def test_the_happy_path_costs_three_calls(deps, operational):
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    assert deps.llm.call_count == 3


async def test_vague_and_scoring_are_issued_before_either_returns(
    leads, operational, monkeypatch
):
    """Counting calls proves how many happened, not how many were in flight.

    The fake holds every call after the first, so when call_count reaches three
    the classifier has returned and the other two are BOTH still open. Three
    sequential calls could never reach that state.
    """
    llm = FakeLLM(*_happy_path(), hold_after=1)
    deps = JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )
    task = asyncio.create_task(
        judge_note(_scope(), _request(), resubmission=False, deps=deps)
    )

    for _ in range(200):
        await asyncio.sleep(0)
        if llm.call_count == 3:
            break

    assert llm.call_count == 3  # both issued...
    llm.released.set()  # ...and only now allowed to return
    judgement = await task
    assert judgement.score is not None


async def test_a_scoring_failure_releases_the_key_exactly_once(
    leads, operational, monkeypatch
):
    releases = {"n": 0}
    real_release = state.release_idempotency

    async def _counting_release(*args, **kwargs):
        releases["n"] += 1
        await real_release(*args, **kwargs)

    monkeypatch.setattr(state, "release_idempotency", _counting_release)

    # Classification fine, vague fine, scoring dies. gather propagates the first
    # exception through the one release-on-error path.
    llm = FakeLLM(
        _classified("discovery"),
        _vague_answer(),
        LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True),
    )
    deps = JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )

    with pytest.raises(ModelUnavailableError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    assert releases["n"] == 1
    assert operational.store == {}


async def test_a_vague_failure_also_releases_the_key(leads, operational):
    llm = FakeLLM(
        _classified("discovery"),
        LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True),
        _score_answer(),
    )
    deps = JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )

    with pytest.raises(ModelUnavailableError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    assert operational.store == {}


async def test_a_suppressing_classification_never_issues_the_other_two(
    leads, operational
):
    llm = FakeLLM(_classified("system_event"))
    deps = JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )

    judgement = await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    assert judgement.suppressed is not None
    assert judgement.suppressed.detail_code is SuppressedDetail.SYSTEM_EVENT
    assert llm.call_count == 1


async def test_a_no_contact_note_is_scored_against_the_narrower_rubric(
    leads, operational, json_capture
):
    llm = FakeLLM(
        _classified("no_contact"),
        _vague_answer(missing=["next_step_with_date"]),
        json_response(
            {"marks": {"what_happened": 20, "next_step_date": 20, "clarity": 8}}
        ),
    )
    deps = JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )

    await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    line = next(x for x in json_capture() if x["message"] == "judgement_completed")
    # 48 of 60 -> 80, good. client_said and deal_specifics left the denominator.
    assert line["denominator"] == 60
    assert line["band"] == "good"
