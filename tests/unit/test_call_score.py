"""call_rubric_v2 (score.py): the model's yes-or-no checks, every mark, total
and band computed in code, the two suppressions, each null reason, the pass
wired into wave 2 only when the call gets a score, and the escalations
agreeing with it (D-75)."""

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
from dodeal_ai.units.call_intelligence.escalations import (
    AGENT_ISSUES,
    Flags,
    escalations_part,
)
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    EXTRAS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.score import (
    CHECK_NAMES,
    ESCALATION_CHECKS,
    PROFESSIONALISM,
    RUBRIC,
    RUBRIC_VERSION,
    ScoreChecks,
    band_of,
    check_score,
    mark,
    reconciled,
    round_half_up,
    score_call,
    score_gate,
    substantive_turns,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import (
    ESCALATIONS,
    OBJECTIONS,
    SCORE,
    wave2,
)
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.wave2_answers import coaching_answer, extras_answer

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
    ("enabled", "language", "eligible", "share", "objections", "reason"),
    [
        (False, True, True, 0.5, True, "scoring_off"),
        (False, False, True, 0.5, True, "scoring_off"),
        (True, False, True, 0.5, True, "language_not_enabled"),
        (True, False, False, 0.1, False, "language_not_enabled"),
        (True, True, False, 0.5, True, "not_eligible"),
        (True, True, True, 0.14, True, "not_engaged"),
        (True, True, True, None, True, "not_engaged"),
        (True, True, True, 0.15, False, "objections_unavailable"),
        (True, True, True, 0.15, True, None),
    ],
)
def test_each_null_reason(
    enabled: bool,
    language: bool,
    eligible: bool,
    share: float | None,
    objections: bool,
    reason: str | None,
) -> None:
    assert (
        score_gate(
            scoring_enabled=enabled,
            language_enabled=language,
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
    llm.script_for(ESCALATIONS_TEMPLATE, json_response({"escalations": []}))
    llm.script_for(COACHING_TEMPLATE, json_response(coaching_answer(segments[0].text)))
    llm.script_for(EXTRAS_TEMPLATE, json_response(extras_answer()))
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
        stage1_escalations=[],
    )


async def test_a_scored_call_runs_the_pass_on_its_own_profile() -> None:
    llm = FakeLLM(json_response(NO_OBJECTIONS), json_response(ALL_YES))
    wave = await _wave2(llm)

    scored = wave.parts[SCORE]
    assert scored is not None and wave.reasons == {}
    assert (scored["raw"], scored["applicable_weight"]) == (30, 60)
    assert (scored["total"], scored["band"]) == (50, "needs_work")
    assert llm.profiles == [
        "unit_b.objections",
        PROFILE_UNIT_B_SCORE,
        "unit_b.escalations",
        "unit_b.coaching",
        "unit_b.extras",
    ]
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
    assert wave.parts[SCORE] is None
    failed = {} if objections else {OBJECTIONS: "objections_malformed_output"}
    assert wave.reasons == {**failed, SCORE: reason}
    asked = ["unit_b.objections"] * (1 if objections else 2)
    assert llm.profiles == [
        *asked,
        "unit_b.escalations",
        "unit_b.coaching",
        "unit_b.extras",
    ]
    assert OBJECTIONS in wave.parts


async def test_a_failed_score_pass_is_null_with_its_reason() -> None:
    llm = FakeLLM(json_response(NO_OBJECTIONS), json_response({}), json_response({}))
    wave = await _wave2(llm)
    assert wave.parts[SCORE] is None
    assert wave.reasons == {SCORE: "score_malformed_output"}
    assert llm.profiles == [
        "unit_b.objections",
        PROFILE_UNIT_B_SCORE,
        PROFILE_UNIT_B_SCORE,
        "unit_b.escalations",
        "unit_b.coaching",
        "unit_b.extras",
    ]


# --- the score keeps its strictness, and gains only the match -------------------


async def test_one_failing_quote_among_true_ones_still_leaves_no_score() -> None:
    """Unlike the extraction, the score keeps nothing field by field: one
    invented quote among three true ones is reprompted, then no score."""
    one_bad = _checks(
        asked_budget="what budget do you have in mind",
        asked_purpose="what budget do you have in mind",
        courteous="Good morning",
        asked_timeline="when do you want to move",
    )
    llm = FakeLLM(
        json_response(NO_OBJECTIONS), json_response(one_bad), json_response(one_bad)
    )

    wave = await _wave2(llm)

    assert wave.parts[SCORE] is None
    assert wave.reasons == {SCORE: "score_malformed_output"}
    assert llm.profiles[:3] == [
        "unit_b.objections",
        PROFILE_UNIT_B_SCORE,
        PROFILE_UNIT_B_SCORE,
    ]


def test_the_score_quote_check_takes_the_match_and_its_neighbours() -> None:
    """A repeated word left out, and a quote found in the segment after the
    one cited, both pass; a dropped negation does not."""
    call = _call(
        _say(0, "agent", "Good morning, what what budget do you have in mind?"),
        _say(5, "lead", "I do not want to wait."),
    )
    kept = ScoreChecks.model_validate(
        _checks(asked_budget="what budget do you have in mind")
    )
    check_score(call)(kept)
    borrowed = _checks(
        courteous={"answer": "yes", "quote": "I do not want", "segment": "s1"}
    )
    check_score(call)(ScoreChecks.model_validate(borrowed))
    dropped = _checks(
        courteous={"answer": "yes", "quote": "I do want", "segment": "s2"}
    )
    with pytest.raises(OutputValidationError) as refused:
        check_score(call)(ScoreChecks.model_validate(dropped))
    assert refused.value.errors == (("courteous", "quote_not_in_segment"),)


# --- engaged: a share, or turns of substance --------------------------------------


def _gate(share: float | None, turns: int, **tenant: Any) -> str | None:
    return score_gate(
        scoring_enabled=True,
        language_enabled=True,
        eligible=True,
        client_share=share,
        objections_answered=True,
        client_turns=turns,
        **tenant,
    )


def test_share_019_with_6_substantive_client_turns_is_engaged() -> None:
    """The guard: engaged at a share of 0.15 or up, or on five turns of four
    words or more -- both hold here."""
    assert _gate(0.19, 6) is None


@pytest.mark.parametrize(
    ("share", "turns", "reason"),
    [
        (0.15, 0, None),
        (0.14, 5, None),
        (0.10, 6, None),
        (0.14, 4, "not_engaged"),
        (None, 9, "not_engaged"),
    ],
    ids=["share-at-floor", "turns-at-floor", "turns-alone", "neither", "no-share"],
)
def test_the_share_or_the_turns_engage(
    share: float | None, turns: int, reason: str | None
) -> None:
    assert _gate(share, turns) == reason


def test_a_tenant_may_move_both_numbers() -> None:
    raised = {"engaged_share": 0.25, "engaged_turns": 7}
    assert _gate(0.19, 6, **raised) == "not_engaged"
    assert _gate(0.19, 7, **raised) is None
    assert _gate(0.25, 0, **raised) is None
    config = CallsConfig(engaged_share=0.25, engaged_turns=7)
    assert (config.engaged_share, config.engaged_turns) == (0.25, 7)
    assert (CallsConfig().engaged_share, CallsConfig().engaged_turns) == (0.15, 5)
    from pydantic import ValidationError

    for refused in ({"engaged_share": 1.5}, {"engaged_turns": 0}):
        with pytest.raises(ValidationError):
            CallsConfig(**refused)


def test_a_turn_is_a_run_of_the_clients_segments_of_four_words_or_more() -> None:
    segments = (
        _say(0, "agent", "Hello, is this a good time?"),
        _say(5, "lead", "Yes."),
        _say(10, "agent", "Great."),
        _say(15, "lead", "I want a villa"),
        _say(20, "agent", "Which area?"),
        _say(25, "lead", "Near the"),
        _say(30, "lead", "marina, please."),
        _say(35, "agent", "Budget?"),
        _say(40, "lead", "Okay thanks"),
    )
    assert substantive_turns(segments, "client") == 2
    assert substantive_turns(segments, "agent") == 1


async def test_a_quiet_client_with_six_real_turns_is_scored_in_wave2() -> None:
    """The client holds 0.10 of the talk but answers in sentences six times:
    engaged, so the score pass runs."""
    said = "I would like to see it"
    quiet = [_say(0, "agent", "Good morning, what budget do you have in mind?", 60)]
    for n in range(6):
        quiet += [
            _say(60 + n * 20, "lead", said, 2),
            _say(62 + n * 20, "agent", "Understood, let me explain the options.", 18),
        ]
    llm = FakeLLM(json_response(NO_OBJECTIONS), json_response(ALL_YES))
    wave = await _wave2(llm, segments=tuple(quiet))
    assert wave.reasons.get(SCORE) is None
    assert PROFILE_UNIT_B_SCORE in llm.profiles


# --- the escalations agree (D-75) ------------------------------------------------------

PROMISE = "I guarantee this villa will double in value"
# An invented call of sixteen segments, the lead's and the agent's in turn;
# the last, s16, is the agent's promise nobody can keep.
LONG = (
    *(
        _say(5 * n, "agent" if n % 2 else "lead", f"Turn {n + 1}, about the villa.")
        for n in range(15)
    ),
    _say(75, "agent", f"{PROMISE} within a year, trust me."),
)
# Understanding 25 (four asked, and listened_more at a 0.5 share), next step
# 13 (two of three), professionalism 15: 53 of 60, 88, excellent.
PASSED = (
    "asked_budget",
    "asked_timeline",
    "asked_purpose",
    "asked_decision_maker",
    "specific_commitment",
    "has_date",
    "courteous",
    "no_over_promise",
    "no_pressure",
)


def _flagged(*flags: dict[str, Any]) -> dict[str, Any]:
    """The escalations part for these model flags on LONG, as wave 2 builds it."""
    answer = Flags.model_validate({"escalations": list(flags)})
    return escalations_part(_call(*LONG), answer, [])


def _promised(segment: str = "s16", quote: str = PROMISE) -> dict[str, Any]:
    return {"issue": "over_promise_or_guarantee", "quote": quote, "segment": segment}


def test_call1s_pattern_an_agents_promise_at_s16_fails_no_over_promise() -> None:
    """The guard: the score said yes, the escalation shows the promise; code
    answers no with the escalation's words and marks the part again."""
    before = _score(_passed(*PASSED))
    assert (before["raw"], before["total"], before["band"]) == (53, 88, "excellent")

    after = reconciled(_call(*LONG), before, _flagged(_promised()))

    assert after is not None
    assert after["components"]["professionalism"] == {
        "weight": 15,
        "mark": 11,
        "checks": {
            "courteous": {"answer": "yes", "quote": None, "segment": None},
            "no_over_promise": {
                "answer": "no",
                "source": "code",
                "quote": PROMISE,
                "segment": "s16",
            },
            "no_pressure": {"answer": "yes", "quote": None, "segment": None},
            "not_interrupting": {"answer": "yes", "source": "code"},
        },
    }
    assert (after["raw"], after["applicable_weight"]) == (49, 60)
    assert (after["total"], after["band"]) == (82, "good")
    for name in ("understanding", "objections", "next_step", "product_knowledge"):
        assert after["components"][name] == before["components"][name]
    # The score it was given is left as it was.
    assert (before["total"], before["components"]["professionalism"]["mark"]) == (
        88,
        15,
    )


def test_the_first_promise_in_time_gives_the_quote() -> None:
    both = _flagged(
        _promised("s14", "Turn 14, about the villa"), _promised("s16", PROMISE)
    )
    after = reconciled(_call(*LONG), _score(_passed(*PASSED)), both)
    assert after is not None
    shown = after["components"]["professionalism"]["checks"]["no_over_promise"]
    assert (shown["quote"], shown["segment"]) == ("Turn 14, about the villa", "s14")
    assert after["components"]["professionalism"]["mark"] == 11


@pytest.mark.parametrize(
    "change",
    [
        {"segment": "s15", "quote": "Turn 15, about the villa", "start_s": 70.0},
        {"speaker": "client"},
        {"speaker": "unknown"},
    ],
    ids=["a-client-segment", "said-by-the-client", "said-by-an-unknown-voice"],
)
def test_an_escalation_the_client_or_an_unknown_voice_said_changes_nothing(
    change: dict[str, Any],
) -> None:
    """The guard: only the agent's own words fail the agent's check. A
    speaker field that says agent is not taken on trust either: the quote is
    found again, in code, in an agent segment."""
    escalated = _flagged(_promised())
    item = escalated["items"][0]
    item.update(change)
    if "segment" in change:
        item["speaker"] = "agent"
    before = _score(_passed(*PASSED))
    assert reconciled(_call(*LONG), before, escalated) == before


@pytest.mark.parametrize(
    "change",
    [
        {"unverified": True},
        {"quote": "I promise you a free car"},
        {"quote": None, "segment": None},
        {"segment": "s99"},
    ],
    ids=["kept-unverified", "not-said", "no-quote", "unknown-segment"],
)
def test_an_unverified_escalation_changes_nothing(change: dict[str, Any]) -> None:
    """The guard: an escalation fails a check only on a quote found, in code,
    where it says it was said."""
    escalated = _flagged(_promised())
    escalated["items"][0].update(change)
    before = _score(_passed(*PASSED))
    assert reconciled(_call(*LONG), before, escalated) == before


def test_with_the_escalations_pass_failed_or_no_score_nothing_changes() -> None:
    """The guard: no escalations part, no change -- the score stays strict."""
    before = _score(_passed(*PASSED))
    assert reconciled(_call(*LONG), before, None) is before
    assert reconciled(_call(*LONG), None, _flagged(_promised())) is None


@pytest.mark.parametrize(
    "issue",
    sorted(
        {"rudeness_or_pressure", "unprofessional_competitor_talk", "possible_broker"}
        | {"wrong_price_or_terms", "qualified_no_next_step"}
    ),
)
def test_an_escalation_with_no_exact_check_changes_nothing(issue: str) -> None:
    """Exact matches only: rudeness_or_pressure may be rudeness alone, so it
    fails neither no_pressure nor courteous."""
    before = _score(_passed(*PASSED))
    segment = "s15" if issue == "possible_broker" else "s16"
    quote = "Turn 15, about the villa" if segment == "s15" else PROMISE
    escalated = _flagged({"issue": issue, "quote": quote, "segment": segment})
    assert reconciled(_call(*LONG), before, escalated) == before


def test_the_mapping_is_exact_and_the_rubric_is_stamped_v2() -> None:
    assert RUBRIC_VERSION == "call_rubric_v2"
    assert ESCALATION_CHECKS == {"over_promise_or_guarantee": "no_over_promise"}
    assert set(ESCALATION_CHECKS) <= AGENT_ISSUES
    assert set(ESCALATION_CHECKS.values()) <= set(PROFESSIONALISM.checks)


def test_a_model_no_already_given_is_answered_again_the_mark_unchanged() -> None:
    """The model said no with the words; code's answer rests on the
    escalation's, and the mark is the same."""
    before = _score(_passed(*(name for name in PASSED if name != "no_over_promise")))
    after = reconciled(_call(*LONG), before, _flagged(_promised()))
    assert after is not None
    shown = after["components"]["professionalism"]
    assert (shown["mark"], shown["checks"]["no_over_promise"]["source"]) == (
        11,
        "code",
    )
    assert (after["total"], after["band"]) == (before["total"], before["band"])


PROMISED_CALL = (
    SEGMENTS[0],
    SEGMENTS[1],
    _say(10, "agent", "I guarantee it doubles in a year, viewing Tuesday at four?"),
    SEGMENTS[3],
)
FLAGGED = {
    "escalations": [
        {
            "issue": "over_promise_or_guarantee",
            "quote": "I guarantee it doubles in a year",
            "segment": "s3",
        }
    ]
}


async def test_wave2_marks_the_score_again_once_the_escalations_answer() -> None:
    """In wave 2: the score pass said no_over_promise yes (50, needs_work);
    the escalations pass flags the agent's promise; the part delivered is
    43, coaching_required."""
    llm = FakeLLM(json_response(NO_OBJECTIONS), json_response(ALL_YES))
    llm.script_for(ESCALATIONS_TEMPLATE, json_response(FLAGGED))

    wave = await _wave2(llm, segments=PROMISED_CALL)

    scored = wave.parts[SCORE]
    assert scored is not None and wave.reasons == {}
    checks = scored["components"]["professionalism"]["checks"]
    assert checks["no_over_promise"] == {
        "answer": "no",
        "source": "code",
        "quote": "I guarantee it doubles in a year",
        "segment": "s3",
    }
    assert scored["components"]["professionalism"]["mark"] == 11
    assert (scored["raw"], scored["applicable_weight"]) == (26, 60)
    assert (scored["total"], scored["band"]) == (43, "coaching_required")


async def test_wave2_with_the_escalations_pass_failed_keeps_the_score() -> None:
    """The guard: two malformed escalations answers leave that part null, and
    the score is the score pass's alone."""
    invented = {
        "escalations": [
            {
                "issue": "over_promise_or_guarantee",
                "quote": "I promise you a free car",
                "segment": "s3",
            }
        ]
    }
    llm = FakeLLM(json_response(NO_OBJECTIONS), json_response(ALL_YES))
    llm.script_for(
        ESCALATIONS_TEMPLATE, json_response(invented), json_response(invented)
    )

    wave = await _wave2(llm, segments=PROMISED_CALL)

    assert wave.parts[ESCALATIONS] is None
    assert wave.reasons == {ESCALATIONS: "escalations_malformed_output"}
    scored = wave.parts[SCORE]
    assert scored is not None
    professionalism = scored["components"]["professionalism"]
    assert professionalism["checks"]["no_over_promise"]["answer"] == "yes"
    assert (professionalism["mark"], scored["total"], scored["band"]) == (
        15,
        50,
        "needs_work",
    )
