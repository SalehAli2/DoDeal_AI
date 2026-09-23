"""unit_b.coaching (coaching.py): a strength and an improvement, each quoted,
a way to say it for every improvement, moments timed in code, a three-action
plan, the call's stages, the summary language, and the tone check (BRD P6)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import MalformedOutputError
from dodeal_ai.core.jobs import JobStatus, Stage2State, create_job, read_job, transition
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_COACHING
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence import coaching
from dodeal_ai.units.call_intelligence.coaching import (
    Coaching,
    check_coaching,
    coach,
    coaching_part,
    harsh,
)
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import COACHING, wave2
from tests.helpers.fake_llm import FakeLLM, json_response

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


def _say(start: float, speaker: str, text: str, language: str = "en") -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language=language,
        confidence=0.9,
    )


# An invented call: a warm opening, good questions, then pressure.
SEGMENTS = (
    _say(0, "agent", "Good morning, thank you for taking my call today."),
    _say(5, "lead", "I am looking for a two bedroom flat near the marina."),
    _say(10, "agent", "What budget do you have, and when do you plan to move?"),
    _say(15, "lead", "About one million, and we move in March."),
    _say(20, "agent", "You have to decide today or the unit is gone."),
    _say(25, "lead", "I need to think about it."),
)
TRANSCRIPT = Transcript.of(SEGMENTS, provider="fake", model="fake")


def _call(*segments: Segment) -> CallText:
    transcript = Transcript.of(segments or SEGMENTS, provider="fake", model="fake")
    return CallText.of(transcript, country_code="971")


def _stage(done: str = "no", quote: str | None = None, segment: str | None = None):
    return {"done": done, "quote": quote, "segment": segment}


STRENGTH = {
    "kind": "strength",
    "text": "Opened warmly and thanked the client for their time.",
    "quote": "thank you for taking my call today",
    "segment": "s1",
    "say_it_like_this": None,
}
IMPROVEMENT = {
    "kind": "improvement",
    "text": "Pressed the client to decide on the spot.",
    "quote": "You have to decide today",
    "segment": "s5",
    "say_it_like_this": "Take the time you need; shall I hold a viewing on Saturday?",
}
ANSWER: dict[str, Any] = {
    "observations": [STRENGTH, IMPROVEMENT],
    "moments": [
        {
            "segment": "s5",
            "what_happened": "The agent pushed for a decision today.",
            "better": "Offer a viewing and a follow-up call instead.",
        }
    ],
    "plan": [
        "Ask about budget and timeline early.",
        "Offer two viewing slots before closing.",
        "Confirm the next step and its date aloud.",
    ],
    "stages": {
        "opening": _stage("yes", "Good morning", "s1"),
        "rapport": _stage(),
        "discovery": _stage("yes", "What budget do you have", "s3"),
        "qualification": _stage("yes", "when do you plan to move", "s3"),
        "presentation": _stage(),
        "objections": _stage(),
        "close": _stage(),
    },
}


def _answer(**changes: Any) -> dict[str, Any]:
    return {**copy.deepcopy(ANSWER), **changes}


def _refused(answer: dict[str, Any], call: CallText | None = None):
    with pytest.raises(OutputValidationError) as refused:
        check_coaching(call or _call())(Coaching.model_validate(answer))
    return refused.value.errors


# --- the guard ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observations", "error"),
    [
        ([IMPROVEMENT, IMPROVEMENT], ("observations", "no_strength")),
        ([STRENGTH, STRENGTH], ("observations", "no_improvement")),
    ],
    ids=["no-strength", "no-improvement"],
)
def test_coaching_with_no_strength_or_no_improvement_is_malformed(
    observations: list, error: tuple[str, str]
) -> None:
    assert _refused(_answer(observations=observations)) == (error,)


@pytest.mark.parametrize(
    ("field", "text", "where"),
    [
        ("observation", "You never listen to the client.", "observations.0"),
        ("plan", "Stop being lazy on the phone.", "plan.1"),
        (
            "say",
            "Your pitch was terrible; say this instead.",
            "observations.1.say_it_like_this",
        ),
    ],
)
def test_a_harsh_phrase_is_caught(field: str, text: str, where: str) -> None:
    answer = _answer()
    if field == "observation":
        answer["observations"][0]["text"] = text
    elif field == "plan":
        answer["plan"][1] = text
    else:
        answer["observations"][1]["say_it_like_this"] = text
    assert _refused(answer) == ((where, "harsh_tone"),)


@pytest.mark.parametrize(
    ("text", "caught"),
    [
        ("You always greet the client warmly.", True),
        ("كان العرض فاشل للأسف", True),
        ("هذا الأسلوب غير مقبول مع العميل", True),
        ("وبدا الوكيل كسول في المتابعة", True),
        ("أداء ممتاز وافتتاح واضح", False),
        ("A strong, clear opening and good questions.", False),
    ],
)
def test_the_tone_list_in_english_and_arabic(text: str, caught: bool) -> None:
    assert harsh(text) is caught
    assert coaching.TONE_LIST_VERSION == "tone_list_v1"


async def test_a_harsh_answer_is_reprompted_once_then_the_pass_fails() -> None:
    answer = _answer(plan=["You never listen.", "Ask more.", "Close better."])
    llm = FakeLLM(json_response(answer), json_response(answer))
    with pytest.raises(MalformedOutputError):
        await coach(llm, _call(), scope=SCOPE, settings=get_settings())
    assert llm.call_count == 2


# --- the rest of the rules --------------------------------------------------------------


def test_a_true_answer_passes_and_its_moments_are_timed_in_code() -> None:
    answer = Coaching.model_validate(ANSWER)
    check_coaching(_call())(answer)
    part = coaching_part(_call(), answer)
    assert part["language"] == "en"
    assert part["moments"] == [
        {"timestamp": "00:20", "start_s": 20.0, **ANSWER["moments"][0]}
    ]
    assert part["plan"] == ANSWER["plan"]
    assert part["stages"]["opening"] == ANSWER["stages"]["opening"]
    assert part["observations"] == [STRENGTH, IMPROVEMENT]


def test_an_improvement_without_a_way_to_say_it_is_malformed() -> None:
    answer = _answer()
    answer["observations"][1]["say_it_like_this"] = None
    assert _refused(answer) == (("observations.1", "say_it_like_this_missing"),)


def test_every_quote_is_checked() -> None:
    answer = _answer()
    answer["observations"][0]["quote"] = "a lovely warm greeting"
    answer["stages"]["close"] = _stage("yes", None, None)
    answer["stages"]["rapport"] = _stage("no", "so nice to meet you", "s1")
    assert _refused(answer) == (
        ("observations.0", "quote_not_in_segment"),
        ("stages.rapport", "quote_not_in_segment"),
        ("stages.close", "quote_missing"),
    )


def test_a_moment_on_a_segment_that_does_not_exist_is_malformed() -> None:
    answer = _answer()
    answer["moments"][0]["segment"] = "s9"
    assert _refused(answer) == (("moments.0", "segment_unknown"),)


def test_coaching_in_the_wrong_language_is_malformed() -> None:
    arabic = tuple(_say(s.start_s, s.speaker, s.text, "ar") for s in SEGMENTS)
    call = _call(*arabic)
    assert call.language == "ar"
    assert _refused(_answer(), call) == (("coaching", "wrong_language"),)


@pytest.mark.parametrize(
    "change",
    [
        {"observations": [STRENGTH]},
        {"observations": [STRENGTH, IMPROVEMENT, STRENGTH, IMPROVEMENT]},
        {"plan": ["one", "two"]},
        {"moments": ANSWER["moments"] * 5},
    ],
    ids=["one-observation", "four-observations", "two-actions", "five-moments"],
)
def test_the_counts_are_held_by_the_schema(change: dict) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Coaching.model_validate(_answer(**change))


# --- in wave 2 -----------------------------------------------------------------------------


async def _wave2(*coaching_answers: Any):
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
    llm = FakeLLM()
    llm.script_for(OBJECTIONS_TEMPLATE, json_response({"objections": []}))
    llm.script_for(ESCALATIONS_TEMPLATE, json_response({"escalations": []}))
    llm.script_for(COACHING_TEMPLATE, *coaching_answers)
    wave = await wave2(
        llm,
        await read_job("tenant-a", "job-1"),
        CallsConfig(),
        TRANSCRIPT,
        work={},
        scope=SCOPE,
        settings=get_settings(),
        usage=PassUsage(),
        eligible=True,
        stage1_escalations=[],
    )
    return llm, wave


async def test_wave2_runs_coaching_on_its_own_profile() -> None:
    llm, wave = await _wave2(json_response(ANSWER))
    part = wave.parts[COACHING]
    assert part is not None and part["plan"] == ANSWER["plan"]
    (sent,) = [c for c in llm.calls if c.profile == PROFILE_UNIT_B_COACHING]
    assert sent.max_output_tokens == 2000


async def test_a_failed_coaching_pass_is_null_with_its_reason() -> None:
    _, wave = await _wave2(json_response({}), json_response({}))
    assert (wave.parts[COACHING], wave.reasons[COACHING]) == (
        None,
        "coaching_malformed_output",
    )
