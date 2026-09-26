"""The two property details (passes.py): property_status (off_plan, ready or
unknown) and handover_date (as said, and an ISO date when clear), each with
evidence like the other details."""

from __future__ import annotations

from typing import Any

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.passes import (
    NOT_MENTIONED,
    STATED,
    UNCERTAIN,
    Extraction,
    check_extraction,
    extract,
    settled,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_passes import _extraction

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")


def _say(start: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="ar",
        confidence=0.9,
    )


# An invented Arabic call about an invented project, then the fixture's four.
SEGMENTS = (
    _say(0, "agent", "Good morning, this is the sales office about the villa."),
    _say(5, "lead", "My budget is 1,200,000 AED, call me on 050 123 4567."),
    _say(10, "agent", "Shall we book the viewing on Tuesday at four?"),
    _say(15, "lead", "Yes, Tuesday works. I am happy with that."),
    _say(20, "agent", "المشروع في طور البناء والتسليم نهاية يونيو ٢٠٢٧"),
    _say(25, "lead", "طيب والشقة الثانية جاهزة للسكن؟"),
)


def _call() -> CallText:
    transcript = Transcript.of(SEGMENTS, provider="fake", model="fake")
    return CallText.of(transcript, country_code="971")


def _status(value: str, state: str, quote: str | None, segment: str | None):
    return {"value": value, "state": state, "quote": quote, "segment": segment}


def _handover(
    value: str | None,
    date: str | None,
    state: str,
    quote: str | None = None,
    segment: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "date": date,
        "state": state,
        "quote": quote,
        "segment": segment,
    }


OFF_PLAN = _status("off_plan", STATED, "في طور البناء", "s5")
HANDOVER = _handover(
    "نهاية يونيو ٢٠٢٧", "2027-06-30", STATED, "والتسليم نهاية يونيو ٢٠٢٧", "s5"
)


def _answer(**details: dict[str, Any]) -> dict[str, Any]:
    answer = _extraction()
    answer["details"].update(details)
    return answer


def _kept(answer: dict[str, Any]) -> dict[str, Any]:
    return settled(Extraction.model_validate(answer), _call())["details"]


# --- the guard ---------------------------------------------------------------------


async def test_under_construction_quoted_gives_off_plan_with_that_quote() -> None:
    """The guard: "في طور البناء" is off_plan, delivered with its own quote."""
    llm = FakeLLM(json_response(_answer(property_status=OFF_PLAN)))

    found, _ = await extract(llm, _call(), scope=SCOPE, settings=get_settings())

    assert llm.call_count == 1
    assert settled(found, _call())["details"]["property_status"] == {
        "value": "off_plan",
        "state": STATED,
        "quote": "في طور البناء",
        "segment": "s5",
        "evidence_failed": False,
    }


# --- the rules ---------------------------------------------------------------------


def test_a_handover_is_kept_as_said_with_its_iso_date() -> None:
    assert _kept(_answer(handover_date=HANDOVER))["handover_date"] == {
        **HANDOVER,
        "evidence_failed": False,
    }


def test_a_handover_said_without_a_clear_date_keeps_the_words_only() -> None:
    vague = _handover("قريب", None, UNCERTAIN)
    assert _kept(_answer(handover_date=vague))["handover_date"]["date"] is None


def test_ready_is_quoted_from_its_own_segment() -> None:
    ready = _status("ready", STATED, "جاهزة للسكن", "s6")
    assert _kept(_answer(property_status=ready))["property_status"]["value"] == "ready"


def test_an_invented_status_quote_leaves_the_status_uncertain_unquoted() -> None:
    invented = _status("ready", STATED, "the villa is ready now", "s5")
    kept = _kept(_answer(property_status=invented))["property_status"]
    assert kept == {
        "value": "ready",
        "state": UNCERTAIN,
        "quote": None,
        "segment": None,
        "evidence_failed": True,
    }


def test_neither_mentioned_is_unknown_and_null() -> None:
    kept = _kept(_extraction())
    assert kept["property_status"] == {
        **_status("unknown", NOT_MENTIONED, None, None),
        "evidence_failed": False,
    }
    assert kept["handover_date"] == {
        **_handover(None, None, NOT_MENTIONED),
        "evidence_failed": False,
    }


def test_an_answer_kept_before_the_two_reads_back_as_not_mentioned() -> None:
    answer = _extraction()
    del answer["details"]["property_status"]
    del answer["details"]["handover_date"]
    details = Extraction.model_validate(answer).details
    assert details.property_status.value == "unknown"
    assert details.handover_date.state == NOT_MENTIONED


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            {"property_status": _status("unknown", STATED, "في طور البناء", "s5")},
            ("details.property_status", "stated_without_value"),
        ),
        (
            {"property_status": _status("off_plan", NOT_MENTIONED, None, None)},
            ("details.property_status", "not_mentioned_with_value"),
        ),
        (
            {"handover_date": _handover("2027", None, NOT_MENTIONED)},
            ("details.handover_date", "not_mentioned_with_value"),
        ),
        (
            {
                "handover_date": _handover(
                    None, "2027-06-30", UNCERTAIN, "في طور البناء", "s5"
                )
            },
            ("details.handover_date", "date_without_value"),
        ),
    ],
    ids=[
        "unknown-stated",
        "status-not-mentioned",
        "handover-not-mentioned",
        "date-no-words",
    ],
)
def test_a_broken_property_detail_is_malformed(
    change: dict[str, Any], error: tuple[str, str]
) -> None:
    with pytest.raises(OutputValidationError) as refused:
        check_extraction(_call())(Extraction.model_validate(_answer(**change)))
    assert refused.value.errors == (error,)


@pytest.mark.parametrize(
    "change",
    [
        {"property_status": _status("under_construction", STATED, "x", "s5")},
        {"handover_date": _handover("2027", "June 2027", STATED, "x", "s5")},
    ],
    ids=["status-not-listed", "date-not-iso"],
)
def test_a_value_off_the_list_or_a_date_not_iso_is_refused_by_the_schema(
    change: dict[str, Any],
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Extraction.model_validate(_answer(**change))
