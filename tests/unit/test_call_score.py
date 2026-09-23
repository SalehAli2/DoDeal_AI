"""call_rubric_v1 (score.py): the model's yes-or-no checks, every mark, total
and band computed in code, the two suppressions, each null reason, and the
pass wired into wave 2 only when the call gets a score."""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.jobs import JobStatus, Stage2State, create_job, read_job, transition
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_SCORE
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.score import (
    CHECK_NAMES,
    RUBRIC,
    ScoreChecks,
    band_of,
    check_score,
    mark,
    round_half_up,
    score_call,
    score_gate,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import OBJECTIONS, SCORE, wave2
from tests.helpers.fake_llm import FakeLLM, json_response

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


def _say(start: float, speaker: str, text: str, seconds: float = 5) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + seconds,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


# An invented call: the agent asks, the client answers, a viewing is set.
SEGMENTS = (
    _say(0, "agent", "Good morning, what budget do you have in mind?"),
    _say(5, "lead", "Around two million, for my family to live in."),
    _say(10, "agent", "Shall I book a viewing on Tuesday at four?"),
    _say(15, "lead", "Yes, Tuesday at four works for me, see you then."),
)


def _call(*segments: Segment) -> CallText:
    transcript = Transcript.of(segments or SEGMENTS, provider="fake", model="fake")
    return CallText.of(transcript, country_code="971")


NO_ANSWER = {"answer": "no", "quote": None, "segment": None}


def _checks(**yes: dict[str, Any] | str) -> dict[str, Any]:
    """Every check no and unquoted -- no_over_promise and no_pressure yes --
    then the named ones as given; a string names a yes with its quote."""
    answer: dict[str, Any] = {name: dict(NO_ANSWER) for name in CHECK_NAMES}
    for name in ("no_over_promise", "no_pressure"):
        answer[name] = {"answer": "yes", "quote": None, "segment": None}
    for name, given in yes.items():
        answer[name] = (
            {"answer": "yes", "quote": given, "segment": "s1"}
            if isinstance(given, str)
            else given
        )
    return answer


def _passed(*names: str) -> ScoreChecks:
    """Checks where exactly `names` pass (no quote check here)."""
    answer = _checks()
    for name in CHECK_NAMES:
        answer[name]["answer"] = "yes" if name in names else "no"
    return ScoreChecks.model_validate(answer)


def _score(
    checks: ScoreChecks,
    *,
    raised: int = 0,
    addressed: int = 0,
    satisfied: int = 0,
    share: float = 0.5,
    interruptions: int = 0,
) -> dict[str, Any]:
    counts = {"raised": raised, "addressed": addressed, "satisfied": satisfied}
    return score_call(
        checks, counts, client_share=share, agent_interruptions=interruptions
    )


def _component(scored: dict[str, Any], name: str) -> dict[str, Any]:
    return scored["components"][name]


# --- a mark is the weight split evenly, rounded half up ---------------------------


@pytest.mark.parametrize(
    ("weight", "passed", "of", "expected"),
    [
        (25, 0, 5, 0),
        (25, 3, 5, 15),
        (25, 5, 5, 25),
        (20, 1, 3, 7),
        (20, 2, 3, 13),
        (15, 1, 4, 4),
        (15, 2, 4, 8),
        (15, 3, 4, 11),
        (15, 1, 2, 8),
        (25, 1, 2, 13),
    ],
)
def test_a_mark_is_the_weight_split_evenly_rounded_half_up(
    weight: int, passed: int, of: int, expected: int
) -> None:
    assert mark(weight, passed, of) == expected


def test_a_half_always_rounds_up_and_nothing_else_does() -> None:
    assert [round_half_up(Fraction(n, 2)) for n in (169, 170, 171)] == [85, 85, 86]
    assert round_half_up(Fraction(8499, 100)) == 85
    assert round_half_up(Fraction(8449, 100)) == 84


# --- one table per component --------------------------------------------------------


@pytest.mark.parametrize(
    ("passed", "share", "expected"),
    [
        ((), 0.39, 0),
        (("asked_budget",), 0.40, 10),
        (("asked_budget", "asked_timeline", "asked_purpose"), 0.2, 15),
        (
            ("asked_budget", "asked_timeline", "asked_purpose", "asked_decision_maker"),
            0.3,
            20,
        ),
        (
            ("asked_budget", "asked_timeline", "asked_purpose", "asked_decision_maker"),
            0.8,
            25,
        ),
    ],
)
def test_understanding(passed: tuple[str, ...], share: float, expected: int) -> None:
    got = _component(_score(_passed(*passed), share=share), "understanding")
    assert (got["weight"], got["mark"]) == (25, expected)
    listened = "yes" if share >= 0.40 else "no"
    assert got["checks"]["listened_more"] == {"answer": listened, "source": "code"}


@pytest.mark.parametrize(
    ("raised", "addressed", "satisfied", "expected"),
    [
        (1, 1, 1, 25),
        (1, 1, 0, 13),
        (2, 1, 1, 13),
        (2, 2, 1, 19),
        (3, 1, 0, 4),
        (2, 0, 0, 0),
    ],
)
def test_objections(raised: int, addressed: int, satisfied: int, expected: int) -> None:
    scored = _score(_passed(), raised=raised, addressed=addressed, satisfied=satisfied)
    got = _component(scored, "objections")
    assert (got["weight"], got["mark"], got["raised"]) == (25, expected, raised)


@pytest.mark.parametrize(
    ("passed", "expected"),
    [
        ((), 0),
        (("specific_commitment",), 7),
        (("specific_commitment", "has_date"), 13),
        (("specific_commitment", "has_date", "named_owner"), 20),
    ],
)
def test_next_step(passed: tuple[str, ...], expected: int) -> None:
    got = _component(_score(_passed(*passed)), "next_step")
    assert (got["weight"], got["mark"]) == (20, expected)


@pytest.mark.parametrize(
    ("passed", "interruptions", "expected"),
    [
        ((), 3, 0),
        ((), 2, 4),
        (("courteous",), 2, 8),
        (("courteous", "no_over_promise", "no_pressure"), 3, 11),
        (("courteous", "no_over_promise", "no_pressure"), 0, 15),
    ],
)
def test_professionalism(
    passed: tuple[str, ...], interruptions: int, expected: int
) -> None:
    got = _component(
        _score(_passed(*passed), interruptions=interruptions), "professionalism"
    )
    assert (got["weight"], got["mark"]) == (15, expected)
    kept = "yes" if interruptions <= 2 else "no"
    assert got["checks"]["not_interrupting"]["answer"] == kept


@pytest.mark.parametrize(
    ("passed", "expected"),
    [
        (("product_question_asked",), 0),
        (("product_question_asked", "answered_directly"), 8),
        (("product_question_asked", "answered_directly", "specific_details"), 15),
    ],
)
def test_product_knowledge(passed: tuple[str, ...], expected: int) -> None:
    got = _component(_score(_passed(*passed)), "product_knowledge")
    assert (got["weight"], got["mark"], got["correctness_unverified"]) == (
        15,
        expected,
        True,
    )


# --- the suppressions, the total and the band ---------------------------------------


def test_no_objection_and_no_product_question_are_suppressed_and_left_out() -> None:
    scored = _score(_passed("courteous"))
    assert _component(scored, "objections") == {
        "weight": 25,
        "mark": None,
        "suppressed": "no_objections",
    }
    assert _component(scored, "product_knowledge") == {
        "weight": 15,
        "mark": None,
        "suppressed": "no_product_question",
    }
    # understanding 5 (listened only) + next step 0 + professionalism 8
    assert (scored["raw"], scored["applicable_weight"]) == (13, 60)
    assert (scored["total"], scored["band"]) == (22, "coaching_required")


def test_the_total_is_raw_over_the_applicable_weight_times_100() -> None:
    scored = _score(_passed(*CHECK_NAMES), raised=2, addressed=2, satisfied=1)
    # 25 + 19 + 20 + 15 + 15 = 94 over 100
    assert (scored["raw"], scored["applicable_weight"]) == (94, 100)
    assert (scored["total"], scored["band"]) == (94, "excellent")
    assert [c.name for c in RUBRIC] == list(scored["components"])


@pytest.mark.parametrize(
    ("total", "band"),
    [
        (100, "excellent"),
        (85, "excellent"),
        (84, "good"),
        (70, "good"),
        (69, "needs_work"),
        (50, "needs_work"),
        (49, "coaching_required"),
        (0, "coaching_required"),
    ],
)
def test_the_bands(total: int, band: str) -> None:
    assert band_of(total) == band


# --- each null reason ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "eligible", "share", "objections", "reason"),
    [
        (False, True, 0.5, True, "scoring_off"),
        (True, False, 0.5, True, "not_eligible"),
        (True, True, 0.19, True, "not_engaged"),
        (True, True, None, True, "not_engaged"),
        (True, True, 0.20, False, "objections_unavailable"),
        (True, True, 0.20, True, None),
    ],
)
def test_each_null_reason(
    enabled: bool,
    eligible: bool,
    share: float | None,
    objections: bool,
    reason: str | None,
) -> None:
    assert (
        score_gate(
            scoring_enabled=enabled,
            eligible=eligible,
            client_share=share,
            objections_answered=objections,
        )
        == reason
    )


# --- the evidence ------------------------------------------------------------------------


def _refused(answer: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    with pytest.raises(OutputValidationError) as refused:
        check_score(_call())(ScoreChecks.model_validate(answer))
    return refused.value.errors


def test_a_quoted_yes_passes() -> None:
    check_score(_call())(
        ScoreChecks.model_validate(_checks(asked_budget="what budget do you have"))
    )


@pytest.mark.parametrize(
    ("name", "given", "error"),
    [
        (
            "asked_budget",
            {"answer": "yes", "quote": None, "segment": None},
            "quote_missing",
        ),
        (
            "no_pressure",
            {"answer": "no", "quote": None, "segment": None},
            "quote_missing",
        ),
        (
            "asked_timeline",
            {"answer": "yes", "quote": "when do you move", "segment": "s1"},
            "quote_not_in_segment",
        ),
        (
            "no_over_promise",
            {"answer": "yes", "quote": "prices will double", "segment": "s3"},
            "quote_not_in_segment",
        ),
    ],
    ids=["yes-unquoted", "pressure-unquoted", "yes-invented", "stray-invented"],
)
def test_a_quote_is_owed_where_the_answer_says_something_was_said(
    name: str, given: dict[str, Any], error: str
) -> None:
    assert _refused(_checks(**{name: given})) == ((name, error),)


# --- in wave 2 ------------------------------------------------------------------------------

NO_OBJECTIONS = {"objections": []}
ALL_YES = _checks(
    asked_budget="what budget do you have in mind",
    asked_purpose="what budget do you have in mind",
    courteous="Good morning",
)


async def _done_job():
    await create_job(
        "tenant-a",
        7,
        job_id="job-1",
        request_id="req-1",
        queue="arq:calls:normal",
        metadata={"author_id": 27, "duration_seconds": 150},
        now=NOW,
    )
    job = await read_job("tenant-a", "job-1")
    assert job is not None
    await transition(
        job, JobStatus.DONE, now=NOW, ttl_seconds=600, stage2=Stage2State.PENDING
    )
    return await read_job("tenant-a", "job-1")


async def _wave2(
    llm: FakeLLM, *, scoring: bool = True, eligible: bool = True, segments=SEGMENTS
):
    return await wave2(
        llm,
        await _done_job(),
        CallsConfig(scoring_enabled=scoring),
        Transcript.of(segments, provider="fake", model="fake"),
        work={},
        scope=SCOPE,
        settings=get_settings(),
        usage=PassUsage(),
        eligible=eligible,
    )


async def test_a_scored_call_runs_the_pass_on_its_own_profile() -> None:
    llm = FakeLLM(json_response(NO_OBJECTIONS), json_response(ALL_YES))
    wave = await _wave2(llm)

    scored = wave.parts[SCORE]
    assert scored is not None and wave.reasons == {}
    assert (scored["raw"], scored["applicable_weight"]) == (30, 60)
    assert (scored["total"], scored["band"]) == (50, "needs_work")
    assert [c.profile for c in llm.calls][1] == PROFILE_UNIT_B_SCORE
    assert llm.calls[1].max_output_tokens == 2500


@pytest.mark.parametrize(
    ("scoring", "eligible", "segments", "objections", "reason"),
    [
        (False, True, SEGMENTS, NO_OBJECTIONS, "scoring_off"),
        (True, False, SEGMENTS, NO_OBJECTIONS, "not_eligible"),
        (
            True,
            True,
            (_say(0, "agent", "Hello?", 30), _say(30, "lead", "Busy.", 2)),
            NO_OBJECTIONS,
            "not_engaged",
        ),
        (True, True, SEGMENTS, {}, "objections_unavailable"),
    ],
    ids=["scoring-off", "not-eligible", "not-engaged", "no-objections-answer"],
)
async def test_a_call_with_no_score_never_runs_the_pass(
    scoring: bool,
    eligible: bool,
    segments: tuple[Segment, ...],
    objections: dict,
    reason: str,
) -> None:
    answers = [json_response(objections)] * (1 if objections else 2)
    llm = FakeLLM(*answers)
    wave = await _wave2(llm, scoring=scoring, eligible=eligible, segments=segments)
    assert (wave.parts[SCORE], wave.reasons[SCORE]) == (None, reason)
    assert all(call.profile != PROFILE_UNIT_B_SCORE for call in llm.calls)
    assert OBJECTIONS in wave.parts


async def test_a_failed_score_pass_is_null_with_its_reason() -> None:
    llm = FakeLLM(json_response(NO_OBJECTIONS), json_response({}), json_response({}))
    wave = await _wave2(llm)
    assert (wave.parts[SCORE], wave.reasons[SCORE]) == (None, "score_malformed_output")
