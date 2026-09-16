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
import time

import pytest
import redis

import dodeal_ai.units.structured_intelligence.pipeline as pipeline_module
from dodeal_ai.core.breaker import operational_breaker
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.cost import limiter
from dodeal_ai.core.errors import (
    BackendUnavailableError,
    DuplicateRequestError,
    IdempotencyUnavailableResponse,
    JudgementDeadlineExceeded,
    MalformedOutputError,
    ModelUnavailableError,
    NoteNotFoundError,
    dodeal_error_response,
)
from dodeal_ai.core.llm import LLMErrorReason, LLMProviderError
from dodeal_ai.core.llm.profiles import (
    PROFILE_UNIT_A_CLASSIFY,
    PROFILE_UNIT_A_SCORE,
    PROFILE_UNIT_A_VAGUE,
)
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.core.prompting import AssembledPrompt, build_prompt
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.middleware import inflight
from dodeal_ai.middleware.inflight import InflightCounter
from dodeal_ai.tools.keys import BackendKeyError
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import (
    CLASSIFY_MAX_OUTPUT_TOKENS,
    CLASSIFY_TEMPLATE,
)
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import (
    JudgementDeps,
    judge_note,
    judge_note_direct,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    DirectJudgementRequest,
    JudgementRequest,
    LeadContext,
    NoteType,
    SuppressedDetail,
)
from dodeal_ai.units.structured_intelligence.scoring import (
    SCORE_MAX_OUTPUT_TOKENS,
    SCORE_TEMPLATE,
)
from dodeal_ai.units.structured_intelligence.vague import (
    VAGUE_MAX_OUTPUT_TOKENS,
    template_for,
)
from tests.helpers import breakers
from tests.helpers.fake_cost_redis import FakeCostRedis
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


@pytest.fixture(autouse=True)
def cost(monkeypatch) -> FakeCostRedis:
    """db1, faked for every test in this file.

    Autouse because the token pre-flight reads it on EVERY judgement and the
    charge writes it on every model response: without this, a test whose
    subject is the reprompt would open a socket to a Redis that is not there.
    """
    client = FakeCostRedis()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: client)
    return client


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


# --- the breaker in front of db2 --------------------------------------------


async def test_an_open_breaker_refuses_the_reservation_at_once(
    deps, operational, json_capture
):
    """An open breaker makes the reservation 503 without a SET reaching the store."""
    await breakers.trip(operational_breaker())

    with pytest.raises(IdempotencyUnavailableResponse):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    assert operational.commands == []
    line = next(x for x in json_capture() if x["message"] == "idempotency_unavailable")
    assert line["breaker"] == "open"


async def test_a_breaker_that_opens_mid_judgement_bypasses_the_rate_limit(
    monkeypatch, deps, operational, json_capture
):
    """A breaker opened by the attempt read refuses the rate-limit trip, and the
    judgement still asks."""
    monkeypatch.setenv("DODEAL_BREAKER_FAILURE_THRESHOLD", "1")
    get_settings.cache_clear()
    operational.raise_on.add("get")

    judgement = await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    assert judgement.decision is not None
    assert judgement.decision.prompt_sent is True
    assert operational.evals == []
    assert [c for c, _ in operational.commands] == ["set", "get"]
    bypass = next(x for x in json_capture() if x["message"] == "rate_limit_bypassed")
    assert bypass["breaker"] == "open"


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
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
    ):
        self.call_count += 1
        if prompt.stable == self._held:
            self.held_calls += 1
            self.entered.set()
            await self.release.wait()
        elif prompt.stable == self._failing:
            await self.entered.wait()
        return await self._inner.complete(
            prompt, profile=profile, max_output_tokens=max_output_tokens
        )


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


# --- elapsed time and in-flight count on the outcome lines (item 72) --------
#
# What is asserted is SHAPE, not duration. A hermetic suite against a fake model
# runs these passes in microseconds, so any threshold would be a test of the
# machine it ran on: "at least a non-negative integer, on every line, on both
# routes, with null where the pass did not run" is the whole claim, and it is
# the claim that catches a field that stopped being emitted or started being
# emitted as a float, a string or a negative.
#
# The three pass fields deliberately are NOT asserted to sum to elapsed_ms.
# Vague detection and scoring overlap -- that is why they are gathered -- so
# their sum can exceed the window they ran inside.

TIMING_FIELDS = ("elapsed_ms", "classify_ms", "vague_ms", "score_ms")


def _outcome(json_capture, message: str = "judgement_completed") -> dict:
    return next(x for x in json_capture() if x["message"] == message)


def _assert_duration(line: dict, field: str) -> None:
    value = line[field]
    # bool is an int in Python and would pass an isinstance check, so it is
    # excluded explicitly: a duration field that started carrying True would be
    # a real bug that a looser assertion would wave through.
    assert isinstance(value, int) and not isinstance(value, bool), (
        f"{field} is {value!r}"
    )
    assert value >= 0, f"{field} is negative: {value}"


async def test_a_scored_judgement_carries_all_five_numbers(
    deps, operational, json_capture
):
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    line = _outcome(json_capture)
    for field in TIMING_FIELDS:
        _assert_duration(line, field)
    _assert_duration(line, "inflight")


async def test_a_suppressed_judgement_carries_elapsed_with_the_passes_null(
    leads, operational, json_capture
):
    # The length gate: nothing ran, so nothing has a duration. Null and not
    # zero -- this note did not take no time to classify, it was never
    # classified, and a zero would average into a latency panel as a fast pass.
    llm = FakeLLM()
    deps = JudgementDeps(
        leads=FakeLeadsClient(
            leads={LEAD_ID: lead(LEAD_ID)}, notes={LEAD_ID: [note(NOTE_ID, "too thin")]}
        ),
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )

    judgement = await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    assert judgement.suppressed is not None

    line = _outcome(json_capture, "judgement_suppressed")
    _assert_duration(line, "elapsed_ms")
    assert line["classify_ms"] is None
    assert line["vague_ms"] is None
    assert line["score_ms"] is None
    _assert_duration(line, "inflight")


async def test_a_classification_suppression_times_the_pass_that_ran(
    leads, operational, json_capture
):
    # One pass ran and two did not, so exactly one of the three is a number.
    # This is the case that would be wrong if the fields were filled in as a
    # block at the end rather than as each pass completed.
    llm = FakeLLM(_classified("system_event"))
    deps = JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )

    await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    line = _outcome(json_capture, "judgement_suppressed")
    _assert_duration(line, "elapsed_ms")
    _assert_duration(line, "classify_ms")
    assert line["vague_ms"] is None
    assert line["score_ms"] is None


async def test_the_numbers_are_the_only_thing_added_to_the_line(
    deps, operational, json_capture
):
    # "Numbers only, nothing else changes on the lines." Everything the line
    # carried before item 72 is still there and unchanged in meaning.
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    line = _outcome(json_capture)
    assert line["tenant"] == "tenant-a"
    assert line["request_id"] == "req-1"
    assert line["note_type"] == "discovery"
    assert line["model_passes"] == 3
    assert line["band"] and line["action"]


class _FakeClock:
    """The pipeline's `time`, with a monotonic the test moves by hand.

    Substituted for the module's `time` global rather than for `time.monotonic`
    itself, because that attribute is shared with asyncio's event loop: a clock
    that jumps twenty milliseconds under the loop's own scheduling is a second
    source of flakiness in place of the first. Every other attribute raises:
    the pipeline reads only `monotonic`, and a second reading must fail loudly
    rather than run on the real clock.
    """

    def __init__(self) -> None:
        # Whole milliseconds from zero, with no base to set: `_ms_since`
        # truncates, and 20 ms read from a float sum or a large base (1000.0)
        # comes back as 19.
        self._ms = 0

    def monotonic(self) -> float:
        return self._ms / 1000

    def advance(self, ms: int) -> None:
        self._ms += ms

    def __getattr__(self, name: str):
        raise AttributeError(
            f"_FakeClock has no {name!r}; the pipeline reads monotonic"
        )


def test_the_fake_clock_refuses_any_attribute_but_monotonic() -> None:
    """Reading any other `time` attribute through the fake clock raises AttributeError."""
    clock = _FakeClock()

    with pytest.raises(AttributeError, match="'time'"):
        _ = clock.time


async def test_elapsed_covers_more_than_any_single_pass(
    leads, operational, json_capture, monkeypatch
):
    """The clock starts at the ENTRY point, not at the first model call.

    Putting a measurable span into the note fetch -- which happens before
    `_judge` is even called -- and asserting `elapsed_ms` reflects it is what
    distinguishes a whole-judgement measurement from one that quietly began
    after the slowest thing the fetch route does.

    The span is ADVANCED on a clock this test owns, never slept: `time.monotonic()`
    advances in ~15.6 ms steps on Windows, so a real 20 ms sleep can measure 15
    and fail an assertion that is correct about code that is correct (register
    item 102). The passes are left on the same clock and never advance it, so
    the twenty milliseconds can only have come from the fetch.
    """
    clock = _FakeClock()
    monkeypatch.setattr(pipeline_module, "time", clock)
    real_get_lead = leads.get_lead

    async def _slow_get_lead(*args, **kwargs):
        clock.advance(20)
        return await real_get_lead(*args, **kwargs)

    monkeypatch.setattr(leads, "get_lead", _slow_get_lead)
    deps = JudgementDeps(
        leads=leads,
        llm=FakeLLM(*_happy_path()),
        config=get_tenant_config("tenant-a"),
        settings=get_settings(),
    )

    await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    line = _outcome(json_capture)
    assert line["elapsed_ms"] >= 20  # the fetch is inside the measurement
    # ...and the passes themselves are not: they ran against a fake and never
    # advanced the clock, so the span cannot have leaked into them.
    assert line["classify_ms"] < 20


async def test_the_in_flight_count_is_read_at_outcome_time(
    deps, operational, json_capture, monkeypatch
):
    # Read from the middleware's counter, not invented here: a judgement run
    # outside a request (as these are) sees whatever the process is actually
    # holding, which is zero.
    counter = InflightCounter()
    monkeypatch.setattr(inflight, "_counter", counter)
    counter.acquire(10)
    counter.acquire(10)

    await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    assert _outcome(json_capture)["inflight"] == 2


async def test_no_timing_field_is_a_wall_clock_timestamp(
    deps, operational, json_capture
):
    # These say how LONG, never when. A field that had picked up a wall-clock
    # reading instead of a duration would be a number in the billions, and it
    # would also be a timestamp on a line that is not supposed to carry one.
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    line = _outcome(json_capture)
    for field in TIMING_FIELDS:
        assert line[field] < 60_000, f"{field} looks like a clock, not a duration"


async def test_the_pipeline_uses_the_monotonic_clock(monkeypatch, deps, operational):
    """The clock itself, pinned.

    `datetime.now()` can go BACKWARDS across an NTP correction, and a negative
    duration in a latency panel is not a small error -- it is a number nobody
    can interpret. Counting the reads proves the module took them from
    `time.monotonic` rather than from a wall clock that happened to agree with
    it on the day the test ran.
    """
    reads = {"n": 0}
    real = time.monotonic

    def _counting():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(pipeline_module.time, "monotonic", _counting)

    await judge_note(_scope(), _request(), resubmission=False, deps=deps)

    # entry, then a start+end for each of the three passes, then the outcome.
    assert reads["n"] >= 8


# --- one deadline per judgement (register item 83) --------------------------

DEADLINE = 0.05
# How long a test waits for the deadline before calling it missing. Generous, so
# a loaded machine cannot fail a correct pipeline -- and finite, so taking the
# deadline out fails the test on its status instead of hanging the suite.
SAFETY_SECONDS = 5.0


class _Unanswered:
    """A model that takes every call and never answers: a provider that has
    stopped responding without closing anything.

    The waiting is FakeLLM's own -- held from the first call on an Event nobody
    sets -- and this wrapper only records whether that wait was CANCELLED, which
    is the difference between a request that stopped and one that let go of the
    provider call and left it running.
    """

    def __init__(self) -> None:
        self.inner = FakeLLM(hold_after=0)
        self.cancelled = 0

    @property
    def call_count(self) -> int:
        return self.inner.call_count

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
    ):
        try:
            return await self.inner.complete(
                prompt, profile=profile, max_output_tokens=max_output_tokens
            )
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def _with_deadline(llm, leads: FakeLeadsClient | None, seconds: float) -> JudgementDeps:
    """Deps whose settings carry `seconds` as the deadline -- through the deps,
    the way the route builds them, not through the process-wide cache."""
    return JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=get_settings().model_copy(
            update={"judgement_deadline_seconds": seconds}
        ),
    )


def _direct_request() -> DirectJudgementRequest:
    return DirectJudgementRequest(
        lead_id=LEAD_ID,
        note_id=NOTE_ID,
        author_id=42,
        note_text=GOOD_NOTE,
        lead=LeadContext(leadType="buyer"),
    )


def _assert_the_deadline_503(exc: JudgementDeadlineExceeded, json_capture) -> None:
    """The error as the CRM receives it, and the one line that says why."""
    rendered = dodeal_error_response(exc, "req-1")
    assert rendered.status_code == 503
    assert json.loads(rendered.body) == {
        "detail": "Service Unavailable",
        "reason": "judgement_deadline_exceeded",
        "request_id": "req-1",
    }

    line = next(
        x for x in json_capture() if x["message"] == "judgement_deadline_exceeded"
    )
    assert line["level"] == "WARNING"
    assert line["reason_code"] == "judgement_deadline_exceeded"
    assert (line["tenant"], line["request_id"]) == ("tenant-a", "req-1")
    _assert_duration(line, "elapsed_ms")
    _assert_duration(line, "inflight")


async def test_a_model_that_never_answers_is_stopped_at_the_deadline(
    leads, operational, json_capture
):
    """The fetch route. Without the deadline this judgement waits out every
    per-call budget in turn while the CRM waits inline on it."""
    llm = _Unanswered()

    with pytest.raises(JudgementDeadlineExceeded) as caught:
        await asyncio.wait_for(
            judge_note(
                _scope(),
                _request(),
                resubmission=False,
                deps=_with_deadline(llm, leads, DEADLINE),
            ),
            SAFETY_SECONDS,
        )

    _assert_the_deadline_503(caught.value, json_capture)
    # The classify call was the one in flight, and it was cancelled -- not
    # abandoned to run on after the 503 had gone out.
    assert (llm.call_count, llm.cancelled) == (1, 1)
    # Item 82 (b): the deadline arrives as a cancellation, and the reservation
    # still goes -- so the CRM's retry is judged, not told 409.
    assert _idem_keys(operational) == []


async def test_the_direct_route_is_stopped_at_the_same_deadline(
    operational, json_capture
):
    """The direct route, which fetches nothing, gets the same deadline, the
    same 503 and the same line."""
    llm = _Unanswered()

    with pytest.raises(JudgementDeadlineExceeded) as caught:
        await asyncio.wait_for(
            judge_note_direct(
                _scope(),
                _direct_request(),
                resubmission=False,
                deps=_with_deadline(llm, None, DEADLINE),
            ),
            SAFETY_SECONDS,
        )

    _assert_the_deadline_503(caught.value, json_capture)
    assert (llm.call_count, llm.cancelled) == (1, 1)
    assert _idem_keys(operational) == []  # item 82 (b), on this route too


async def test_the_fetch_spends_the_same_deadline(
    leads, operational, json_capture, monkeypatch
):
    """The deadline is armed at the entry point, OUTSIDE _judge, so a backend
    read that never returns is stopped by it too -- and no model is called."""
    never = asyncio.Event()

    async def _hung_get_lead(*args, **kwargs):
        await never.wait()

    monkeypatch.setattr(leads, "get_lead", _hung_get_lead)
    llm = FakeLLM(*_happy_path())

    with pytest.raises(JudgementDeadlineExceeded) as caught:
        await asyncio.wait_for(
            judge_note(
                _scope(),
                _request(),
                resubmission=False,
                deps=_with_deadline(llm, leads, DEADLINE),
            ),
            SAFETY_SECONDS,
        )

    _assert_the_deadline_503(caught.value, json_capture)
    assert llm.call_count == 0


@pytest.mark.parametrize("route", ["fetch", "direct"])
async def test_a_judgement_inside_its_deadline_is_unaffected(
    route, leads, operational, json_capture
):
    """A deadline that is armed and not reached changes nothing: the same
    judgement, the same outcome line, and no deadline line."""
    llm = FakeLLM(*_happy_path())
    if route == "fetch":
        judgement = await judge_note(
            _scope(),
            _request(),
            resubmission=False,
            deps=_with_deadline(llm, leads, SAFETY_SECONDS),
        )
    else:
        judgement = await judge_note_direct(
            _scope(),
            _direct_request(),
            resubmission=False,
            deps=_with_deadline(llm, None, SAFETY_SECONDS),
        )

    assert judgement.score is not None and judgement.decision is not None
    assert llm.call_count == 3
    messages = _messages(json_capture)
    assert "judgement_completed" in messages
    assert "judgement_deadline_exceeded" not in messages


# --- the reservation: released on any exit, short until judged (item 82) ----

LONG_TTL = get_tenant_config("tenant-a").idempotency_ttl_seconds


def _idem_keys(operational: FakeOperationalRedis) -> list[str]:
    return [key for key in operational.store if key.startswith("idem:")]


async def _until_the_first_call(llm) -> None:
    """Run the loop until the classify call is in flight, and no further."""
    for _ in range(200):
        await asyncio.sleep(0)
        if llm.call_count == 1:
            return
    raise AssertionError("the classify call was never issued")


async def test_a_request_cancelled_mid_classify_releases_the_reservation(
    leads, operational, monkeypatch
):
    """Item 82 (a). A cancellation is a BaseException, so a release that ran on
    Exception alone left the key behind, and every retry met 409.

    The cancellation lands TWICE here, the second while the release's DELETE is
    on the wire. A worker shutdown does that, and so does a cancel scope that
    re-delivers on every await. It is what the shield is for: without it, the
    second cancellation kills the DELETE in flight and the key survives.
    """
    llm = FakeLLM(*_happy_path(), hold_after=0)
    task = asyncio.create_task(
        judge_note(
            _scope(), _request(), resubmission=False, deps=_deps_with(llm, leads)
        )
    )
    await _until_the_first_call(llm)
    assert len(_idem_keys(operational)) == 1  # reserved, and classify is out

    real_delete = operational.delete
    delete_sent = asyncio.Event()

    async def _delete_on_the_wire(*names: str) -> int:
        delete_sent.set()
        await asyncio.sleep(0)  # the round trip: where the second cancel lands
        return await real_delete(*names)

    monkeypatch.setattr(operational, "delete", _delete_on_the_wire)

    task.cancel()
    await asyncio.wait_for(delete_sent.wait(), SAFETY_SECONDS)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await _drain()

    assert _idem_keys(operational) == []


async def test_the_reservation_is_short_while_the_judgement_runs(leads, operational):
    """Item 82 (d). Taken for four deadlines -- 100 s against a 25 s deadline --
    so a worker killed mid-judgement cannot lock the note for a day."""
    llm = FakeLLM(*_happy_path(), hold_after=0)
    task = asyncio.create_task(
        judge_note(
            _scope(),
            _request(),
            resubmission=False,
            deps=_with_deadline(llm, leads, 25.0),
        )
    )
    await _until_the_first_call(llm)

    [key] = _idem_keys(operational)
    assert (operational.store[key], operational.ttls[key]) == ("1", 100)

    llm.released.set()
    await task
    assert (operational.store[key], operational.ttls[key]) == ("done", LONG_TTL)


@pytest.mark.parametrize("classified", ["discovery", "system_event"])
async def test_a_judgement_confirms_the_reservation_for_the_long_ttl(
    classified, leads, operational
):
    """Item 82 (c). Once the judgement exists the key holds "done" for the
    tenant's long TTL -- a classifier suppression exists as much as a score
    does, so it confirms the same way."""
    script = (
        _happy_path(classified)
        if classified == "discovery"
        else [_classified(classified)]
    )
    judgement = await judge_note(
        _scope(),
        _request(),
        resubmission=False,
        deps=_deps_with(FakeLLM(*script), leads),
    )
    assert (judgement.suppressed is None) is (classified == "discovery")

    [key] = _idem_keys(operational)
    assert (operational.store[key], operational.ttls[key]) == ("done", LONG_TTL)


async def test_a_confirmed_judgement_is_still_a_duplicate(deps, operational):
    """Item 82 (f), from the other side: the confirm REPLACES the reservation,
    so the second identical request still meets 409."""
    await judge_note(_scope(), _request(), resubmission=False, deps=deps)
    [key] = _idem_keys(operational)
    assert operational.store[key] == "done"

    with pytest.raises(DuplicateRequestError):
        await judge_note(_scope(), _request(), resubmission=False, deps=deps)


async def test_a_confirm_that_fails_still_returns_the_judgement(
    leads, operational, json_capture, monkeypatch
):
    """Item 82 (e). The judgement is built and paid for, so a store that refuses
    the confirm costs a log line and a short-lived key -- never the answer."""
    real_set = operational.set

    async def _refuse_the_confirm(name, value, nx=False, ex=None, xx=False):
        if xx:
            raise redis.ConnectionError("confirm refused")
        return await real_set(name, value, nx=nx, ex=ex, xx=xx)

    monkeypatch.setattr(operational, "set", _refuse_the_confirm)

    judgement = await judge_note(
        _scope(),
        _request(),
        resubmission=False,
        deps=_with_deadline(FakeLLM(*_happy_path()), leads, 25.0),
    )

    assert judgement.score is not None and judgement.decision is not None
    messages = _messages(json_capture)
    assert "judgement_completed" in messages
    line = next(
        x for x in json_capture() if x["message"] == "idempotency_confirm_bypassed"
    )
    assert line["reason_code"] == "idempotency_confirm_bypassed"
    assert (line["tenant"], line["request_id"]) == ("tenant-a", "req-1")
    # Neither released nor confirmed: left to expire on its in-flight TTL.
    [key] = _idem_keys(operational)
    assert (operational.store[key], operational.ttls[key]) == ("1", 100)


# --- each pass names its own profile (Piece M, report R17) ------------------

# Profile per TEMPLATE, which is the pairing that matters. Asserting against
# arrival order would re-pin the gather's scheduling accident that `script_for`
# exists to remove: vague and scoring are issued together and either may land
# first.
_PROFILE_BY_TEMPLATE = {
    CLASSIFY_TEMPLATE: PROFILE_UNIT_A_CLASSIFY,
    VAGUE_TEMPLATE: PROFILE_UNIT_A_VAGUE,
    SCORE_TEMPLATE: PROFILE_UNIT_A_SCORE,
}


async def test_each_pass_names_its_own_profile(operational, leads):
    """One scored judgement names classify, vague and score -- each paired with
    the template that was actually sent."""
    llm = FakeLLM()
    llm.script_for(CLASSIFY_TEMPLATE, _classified("discovery"))
    llm.script_for(VAGUE_TEMPLATE, _vague_answer())
    llm.script_for(SCORE_TEMPLATE, _score_answer())

    judgement = await judge_note(
        _scope(), _request(), resubmission=False, deps=_deps_with(llm, leads)
    )
    assert judgement.suppressed is None and judgement.score is not None

    stable_to_profile = {
        build_prompt(template, "").stable: profile
        for template, profile in _PROFILE_BY_TEMPLATE.items()
    }
    paired = {stable_to_profile[call.prompt.stable]: call.profile for call in llm.calls}
    assert paired == {
        PROFILE_UNIT_A_CLASSIFY: PROFILE_UNIT_A_CLASSIFY,
        PROFILE_UNIT_A_VAGUE: PROFILE_UNIT_A_VAGUE,
        PROFILE_UNIT_A_SCORE: PROFILE_UNIT_A_SCORE,
    }
    assert llm.call_count == 3


async def test_the_reprompt_runs_on_the_same_profile(operational, leads):
    # The second attempt is the same task said more strictly. A different
    # profile there would make the reprompt a second variable.
    llm = FakeLLM()
    llm.script_for(CLASSIFY_TEMPLATE, _classified("discovery"))
    llm.script_for(VAGUE_TEMPLATE, response("not json at all"), _vague_answer())
    llm.script_for(SCORE_TEMPLATE, _score_answer())

    await judge_note(
        _scope(), _request(), resubmission=False, deps=_deps_with(llm, leads)
    )

    vague_stable = build_prompt(VAGUE_TEMPLATE, "").stable
    vague_calls = [c for c in llm.calls if c.prompt.stable == vague_stable]
    assert len(vague_calls) == 2
    assert {c.profile for c in vague_calls} == {PROFILE_UNIT_A_VAGUE}


async def test_each_pass_sends_its_own_task_ceiling(operational, leads):
    # The three ceilings are per task and unchanged by Piece M: a profile may
    # lower one at the adapter, never at the call site.
    llm = FakeLLM()
    llm.script_for(CLASSIFY_TEMPLATE, _classified("discovery"))
    llm.script_for(VAGUE_TEMPLATE, _vague_answer())
    llm.script_for(SCORE_TEMPLATE, _score_answer())

    await judge_note(
        _scope(), _request(), resubmission=False, deps=_deps_with(llm, leads)
    )

    ceilings = {
        build_prompt(template, "").stable: ceiling
        for template, ceiling in (
            (CLASSIFY_TEMPLATE, CLASSIFY_MAX_OUTPUT_TOKENS),
            (VAGUE_TEMPLATE, VAGUE_MAX_OUTPUT_TOKENS),
            (SCORE_TEMPLATE, SCORE_MAX_OUTPUT_TOKENS),
        )
    }
    for call in llm.calls:
        assert call.max_output_tokens == ceilings[call.prompt.stable]
