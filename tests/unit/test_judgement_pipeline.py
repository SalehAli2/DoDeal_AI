"""judge_note called directly, for the contracts the routes reach awkwardly.

The release-on-failure path is the important one. Through HTTP it is reachable
only through a model failure, which is one specific way of failing after the
reservation; here the failure is INJECTED at the token pre-flight, so the
release is exercised for "anything at all raised after we reserved" rather than
for the one cause the route tests happen to have. It is the difference between
"retry your request" and "409 for the next 24 hours".

The last section is register item 63: when one of the two concurrent passes
fails, the OTHER one must stop. That claim is only testable if the surviving
pass would have done something observable had it been left alone -- so the held
pass in those tests is scripted to come back MALFORMED, which under the old
`asyncio.gather` meant a `reprompt_issued` line and a fourth model call charged
to a request that had already 503'd.
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
    MalformedOutputError,
    ModelUnavailableError,
    NoteNotFoundError,
)
from dodeal_ai.core.llm import LLMErrorReason, LLMProviderError
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.core.prompting import AssembledPrompt, build_prompt
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.tools.keys import BackendKeyError
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import JudgementDeps, judge_note
from dodeal_ai.units.structured_intelligence.schemas import (
    JudgementRequest,
    NoteType,
    SuppressedDetail,
)
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response, response
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


# --- one pass fails, the other is cancelled (register item 63) --------------

# A note body shaped like the ones this service really reads. It travels through
# the FAILING pass in every test below, so if any of them can find it in a log
# line, a real note reached the log on the failure path.
SENTINEL = "SENTINEL-0501234567 villa budget 4.2M"

VAGUE_TEMPLATE = template_for(NoteType.DISCOVERY)


class _OnePassHeld:
    """A client where one of the two concurrent passes is HELD OPEN.

    Delegates to a real `FakeLLM` for every answer; the only thing it adds is
    ordering, because "the sibling was still in flight when the failure arrived"
    must be a fact of the test and not a property of the event loop:

      the HELD template    records the call, announces that it has been entered,
                           and then waits on `release` -- which the TEST sets,
                           after the request has already failed.
      the FAILING template waits for that announcement before delegating, so it
                           cannot fail before its sibling has been issued.

    `release` is what gives these tests their teeth. A cancelled task is gone by
    the time it is set and nothing further happens; a LEAKED one wakes up, gets
    its malformed answer, logs `reprompt_issued` and spends a fourth call. So
    the assertions made after `release.set()` are the ones that would fail if
    the cancellation were taken out again.
    """

    def __init__(self, inner: FakeLLM, *, held: str, failing: str) -> None:
        self._inner = inner
        self._held = build_prompt(held, "").stable
        self._failing = build_prompt(failing, "").stable
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.held_calls = 0
        # Counted HERE and not read off the inner fake, which only records a
        # call once it is allowed to answer -- and a held call has not been.
        # This is what lets a test say "no second call was made for that pass"
        # rather than the weaker "no second answer was given".
        self.call_count = 0

    async def complete(
        self, prompt: AssembledPrompt, *, max_output_tokens: int | None = None
    ):
        self.call_count += 1
        if prompt.stable == self._held:
            self.held_calls += 1
            self.entered.set()
            await self.release.wait()
        elif prompt.stable == self._failing:
            await self.entered.wait()
        return await self._inner.complete(prompt, max_output_tokens=max_output_tokens)


def _deps_with(llm, leads: FakeLeadsClient) -> JudgementDeps:
    return JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )


def _sentinel_leads() -> FakeLeadsClient:
    return FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)},
        notes={LEAD_ID: [note(NOTE_ID, f"{SENTINEL} Following up Tuesday at 3pm.")]},
    )


def _held_scoring(*, vague: list, score: list) -> _OnePassHeld:
    """The pair with SCORING held open and vague detection free to fail."""
    inner = FakeLLM()
    inner.script_for(CLASSIFY_TEMPLATE, _classified("discovery"))
    inner.script_for(VAGUE_TEMPLATE, *vague)
    inner.script_for(SCORE_TEMPLATE, *score)
    return _OnePassHeld(inner, held=SCORE_TEMPLATE, failing=VAGUE_TEMPLATE)


def _unavailable() -> LLMProviderError:
    return LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True)


# Malformed, then good: what an abandoned scoring pass WOULD do if it were left
# running. The second entry is only ever reached if the cancellation failed.
def _malformed_then_good() -> list:
    return [response("not json at all"), _score_answer()]


async def _drain(loops: int = 200) -> None:
    """Give the loop every chance to run a task that was NOT cancelled.

    Without this the leak assertions would be a race the leak usually wins: they
    would run before the abandoned pass had got as far as its reprompt, and a
    test that passes because it finished first proves nothing.
    """
    for _ in range(loops):
        await asyncio.sleep(0)


def _messages(json_capture) -> list[str]:
    return [x["message"] for x in json_capture()]


def _assert_sentinel_absent(json_capture) -> None:
    text = json.dumps(json_capture())
    assert "SENTINEL" not in text
    assert "0501234567" not in text


async def test_a_vague_failure_cancels_the_scoring_pass(operational, json_capture):
    llm = _held_scoring(vague=[_unavailable()], score=_malformed_then_good())
    deps = _deps_with(llm, _sentinel_leads())

    with pytest.raises(ModelUnavailableError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    assert llm.held_calls == 1  # the scoring pass was issued exactly once...
    assert llm.call_count == 3  # ...so classify, vague, score, and nothing more

    llm.release.set()
    await _drain()

    # Nothing woke up: the task was cancelled, not merely abandoned. A leaked
    # one would have taken its malformed answer and spent a fourth call on it.
    assert llm.held_calls == 1
    assert llm.call_count == 3
    assert "reprompt_issued" not in _messages(json_capture)


async def test_cancelling_the_sibling_still_releases_the_key(operational):
    # Cancelling the sibling must not step over the release-on-error block --
    # the 503 is ours, so the caller must be able to retry immediately.
    llm = _held_scoring(vague=[_unavailable()], score=_malformed_then_good())

    with pytest.raises(ModelUnavailableError):
        await judge_note(
            _scope(),
            _request(),
            resubmission=False,
            deps=_deps_with(llm, _sentinel_leads()),
        )

    assert operational.store == {}


async def test_the_cancelled_pass_puts_no_note_text_in_a_log_line(
    operational, json_capture
):
    # The note is in flight in BOTH passes when one of them dies. A cancellation
    # unwinds two coroutines that are holding it, and a traceback or a %r on the
    # way out is exactly how it would reach a line.
    llm = _held_scoring(vague=[_unavailable()], score=_malformed_then_good())

    with pytest.raises(ModelUnavailableError):
        await judge_note(
            _scope(),
            _request(),
            resubmission=False,
            deps=_deps_with(llm, _sentinel_leads()),
        )
    llm.release.set()
    await _drain()

    _assert_sentinel_absent(json_capture)


async def test_a_scoring_failure_cancels_the_vague_pass(operational, json_capture):
    # The mirror. Which side fails must not decide whether the other one stops.
    inner = FakeLLM()
    inner.script_for(CLASSIFY_TEMPLATE, _classified("discovery"))
    inner.script_for(SCORE_TEMPLATE, _unavailable())
    inner.script_for(VAGUE_TEMPLATE, response("not json at all"), _vague_answer())
    llm = _OnePassHeld(inner, held=VAGUE_TEMPLATE, failing=SCORE_TEMPLATE)

    with pytest.raises(ModelUnavailableError):
        await judge_note(
            _scope(),
            _request(),
            resubmission=False,
            deps=_deps_with(llm, _sentinel_leads()),
        )

    assert llm.held_calls == 1
    llm.release.set()
    await _drain()

    assert llm.held_calls == 1
    assert llm.call_count == 3
    assert "reprompt_issued" not in _messages(json_capture)
    assert operational.store == {}
    _assert_sentinel_absent(json_capture)


async def test_a_malformed_pass_that_503s_also_cancels_its_sibling(
    operational, json_capture
):
    # The other 503 this gather can produce. The failing pass is malformed
    # twice, so it costs two calls and leaves as malformed_output rather than
    # model_unavailable; the sibling must stop for that failure too.
    llm = _held_scoring(
        vague=[response("not json at all"), response("still not json")],
        score=_malformed_then_good(),
    )

    with pytest.raises(MalformedOutputError):
        await judge_note(
            _scope(),
            _request(),
            resubmission=False,
            deps=_deps_with(llm, _sentinel_leads()),
        )

    llm.release.set()
    await _drain()

    assert llm.held_calls == 1  # scoring was issued once and never reprompted
    # classify + the two vague calls + the one held scoring call. The vague
    # reprompt is the pass that FAILED, and it is entitled to its second attempt.
    assert llm.call_count == 4
    assert _messages(json_capture).count("reprompt_issued") == 1
    assert operational.store == {}


async def test_the_happy_path_still_returns_both_results_in_order(deps, operational):
    # gather_or_cancel replaced asyncio.gather at one call site, and the thing
    # most easily broken by that swap is the unpacking on the line below it:
    # two results, vague first, scoring second.
    judgement = await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    assert judgement.suppressed is None
    assert judgement.analysis.is_vague is True  # from the VAGUE result
    assert judgement.score is not None and judgement.score.denominator == 80
