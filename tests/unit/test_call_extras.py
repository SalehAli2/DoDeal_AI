"""unit_b.extras (extras.py): keywords checked in their segment, the three tags,
a WhatsApp suggestion of at most 60 words in the summary language and never
sent by us, and seriousness banded in code and marked manager_only."""

from __future__ import annotations

import ast
import copy
import pathlib
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import MalformedOutputError
from dodeal_ai.core.jobs import JobStatus, Stage2State, create_job, read_job, transition
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_EXTRAS
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.extras import (
    SERIOUSNESS_CHECKS,
    Extras,
    check_extras,
    extras_part,
    find_extras,
    seriousness_band,
)
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    EXTRAS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import EXTRAS, wave2
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.wave2_answers import coaching_answer

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
SRC = pathlib.Path("src/dodeal_ai")


def _say(start: float, speaker: str, text: str, language: str = "en") -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language=language,
        confidence=0.9,
    )


# An invented call about an invented project.
SEGMENTS = (
    _say(0, "agent", "Good morning, calling about the Palm Grove Residences."),
    _say(5, "lead", "My budget is about two million, and I move in March."),
    _say(10, "lead", "My wife decides with me, we both want a garden."),
    _say(15, "agent", "Shall I book a viewing on Saturday at ten?"),
    _say(20, "lead", "Yes, Saturday works for us."),
)
TRANSCRIPT = Transcript.of(SEGMENTS, provider="fake", model="fake")


def _call(*segments: Segment) -> CallText:
    transcript = Transcript.of(segments or SEGMENTS, provider="fake", model="fake")
    return CallText.of(transcript, country_code="971")


def _check(answer: str = "no", quote: str | None = None, segment: str | None = None):
    return {"answer": answer, "reason": "As said.", "quote": quote, "segment": segment}


ANSWER: dict[str, Any] = {
    "keywords": [
        {
            "kind": "project",
            "said": "Palm Grove Residences",
            "english": "Palm Grove Residences",
            "segment": "s1",
        },
        {"kind": "topic", "said": "a garden", "english": None, "segment": "s3"},
    ],
    "tags": {"outcome": "moved_forward", "stage": "viewing", "client_type": "end_user"},
    "whatsapp": "Thank you both. Your viewing is set for Saturday; see you there!",
    "seriousness": {
        "budget_stated": _check("yes", "My budget is about two million", "s2"),
        "timeline_stated": _check("yes", "I move in March", "s2"),
        "decision_maker_named": _check("yes", "My wife decides with me", "s3"),
        "next_step_agreed": _check("yes", "Saturday works for us", "s5"),
        "client_engaged": _check(),
    },
}


def _answer(**changes: Any) -> dict[str, Any]:
    return {**copy.deepcopy(ANSWER), **changes}


def _refused(answer: dict[str, Any], call: CallText | None = None):
    with pytest.raises(OutputValidationError) as refused:
        check_extras(call or _call())(Extras.model_validate(answer))
    return refused.value.errors


def _with_yes(count: int) -> dict[str, Any]:
    """Seriousness with the first `count` checks yes, each truly quoted."""
    quotes = [
        ("My budget is about two million", "s2"),
        ("I move in March", "s2"),
        ("My wife decides with me", "s3"),
        ("Saturday works for us", "s5"),
        ("we both want a garden", "s3"),
    ]
    return {
        name: _check("yes", *quotes[n]) if n < count else _check()
        for n, name in enumerate(SERIOUSNESS_CHECKS)
    }


# --- the guard: the band table and the 60-word cap -----------------------------------


@pytest.mark.parametrize(
    ("yes", "band"),
    [(0, "C"), (1, "C"), (2, "B"), (3, "B"), (4, "A"), (5, "A")],
)
def test_the_band_is_computed_in_code_from_the_yes_count(yes: int, band: str) -> None:
    answer = Extras.model_validate(_answer(seriousness=_with_yes(yes)))
    check_extras(_call())(answer)
    seriousness = extras_part(_call(), answer)["seriousness"]
    assert (seriousness["band"], seriousness["yes"]) == (band, yes)
    assert seriousness_band(yes) == band


def test_a_whatsapp_suggestion_over_60_words_is_malformed() -> None:
    sixty = " ".join(["word"] * 60)
    check_extras(_call())(Extras.model_validate(_answer(whatsapp=sixty)))
    assert _refused(_answer(whatsapp=f"{sixty} more")) == (("whatsapp", "too_long"),)


async def test_a_long_whatsapp_suggestion_is_reprompted_once_then_fails() -> None:
    long = _answer(whatsapp=" ".join(["word"] * 61))
    llm = FakeLLM(json_response(long), json_response(long))
    with pytest.raises(MalformedOutputError):
        await find_extras(llm, _call(), scope=SCOPE, settings=get_settings())
    assert llm.call_count == 2


# --- the rest of the rules --------------------------------------------------------------


def test_a_true_answer_passes_and_is_marked_manager_only() -> None:
    answer = Extras.model_validate(ANSWER)
    check_extras(_call())(answer)
    part = extras_part(_call(), answer)
    assert part["keywords"] == [
        {**keyword, "canonical": None} for keyword in ANSWER["keywords"]
    ]
    assert part["tags"] == ANSWER["tags"]
    assert part["whatsapp_suggestion"] == {"language": "en", "text": ANSWER["whatsapp"]}
    seriousness = part["seriousness"]
    assert (seriousness["band"], seriousness["manager_only"]) == ("A", True)
    assert seriousness["checks"] == ANSWER["seriousness"]


def test_a_keyword_not_said_in_its_segment_is_malformed() -> None:
    answer = _answer()
    answer["keywords"][0]["said"] = "Marina Heights"
    answer["keywords"][1]["segment"] = "s9"
    assert _refused(answer) == (
        ("keywords.0", "quote_not_in_segment"),
        ("keywords.1", "segment_unknown"),
    )


def test_every_seriousness_quote_is_checked_and_a_yes_is_owed_one() -> None:
    answer = _answer()
    answer["seriousness"]["budget_stated"] = _check("yes")
    answer["seriousness"]["client_engaged"] = _check("no", "we love it", "s5")
    assert _refused(answer) == (
        ("seriousness.budget_stated", "quote_missing"),
        ("seriousness.client_engaged", "quote_not_in_segment"),
    )


def test_a_whatsapp_suggestion_in_the_wrong_language_is_malformed() -> None:
    arabic = tuple(_say(s.start_s, s.speaker, s.text, "ar") for s in SEGMENTS)
    call = _call(*arabic)
    assert call.language == "ar"
    answer = _answer(seriousness=_with_yes(0), keywords=[])
    assert _refused(answer, call) == (("whatsapp", "wrong_language"),)
    arabic_text = _answer(
        seriousness=_with_yes(0), keywords=[], whatsapp="شكرا لوقتك، نراك يوم السبت."
    )
    check_extras(call)(Extras.model_validate(arabic_text))


@pytest.mark.parametrize(
    "change",
    [
        {"tags": {"outcome": "won", "stage": "viewing", "client_type": "unknown"}},
        {"keywords": ANSWER["keywords"] * 8},
        {"whatsapp": ""},
        {"reasoning": "hidden thoughts"},
    ],
    ids=["unknown-tag", "sixteen-keywords", "empty-message", "extra-field"],
)
def test_the_shape_is_held_by_the_schema(change: dict) -> None:
    with pytest.raises(ValidationError):
        Extras.model_validate(_answer(**change))


def test_the_whatsapp_suggestion_has_no_way_out_but_the_callback() -> None:
    """LLM06: the service's only POSTs are the signed callback and the model
    call, and the extras module imports nothing that could send."""
    posts = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if re.search(r"(?<!router)\.post\(", path.read_text(encoding="utf-8"))
    )
    assert posts == ["core/callbacks.py", "core/llm/openai_compatible.py"]
    tree = ast.parse((SRC / "units/call_intelligence/extras.py").read_text("utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    senders = ("httpx", "callbacks", "delivery", "queues", "urllib", "smtplib")
    assert not [name for name in imported if name.endswith(senders)]


# --- in wave 2 -----------------------------------------------------------------------------


async def _wave2(*extras_answers: Any):
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
    llm.script_for(COACHING_TEMPLATE, json_response(coaching_answer(SEGMENTS[0].text)))
    llm.script_for(EXTRAS_TEMPLATE, *extras_answers)
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


async def test_wave2_runs_extras_last_on_its_own_profile() -> None:
    llm, wave = await _wave2(json_response(ANSWER))
    part = wave.parts[EXTRAS]
    assert part is not None and part["tags"] == ANSWER["tags"]
    assert llm.profiles[-1] == PROFILE_UNIT_B_EXTRAS
    assert llm.calls[-1].max_output_tokens == 2000
    assert wave.reasons == {"score": "scoring_off"}


async def test_a_failed_extras_pass_is_null_and_the_rest_stands() -> None:
    _, wave = await _wave2(json_response({}), json_response({}))
    assert (wave.parts[EXTRAS], wave.reasons[EXTRAS]) == (
        None,
        "extras_malformed_output",
    )
    assert wave.parts["coaching"] is not None
    assert wave.parts["objections"] is not None
