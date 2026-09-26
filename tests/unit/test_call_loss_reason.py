"""A dead call's loss reason (passes.py): one of the nine objection categories
or no_reason_given, with the client's quote when there is one; and the
coaching tip it brings (coaching.py)."""

from __future__ import annotations

from typing import Any, get_args

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence import passes
from dodeal_ai.units.call_intelligence.coaching import (
    ASK_WHY,
    Coaching,
    coaching_part,
    harsh,
)
from dodeal_ai.units.call_intelligence.evidence import CallText, in_language
from dodeal_ai.units.call_intelligence.objections import OBJECTION_CATEGORIES
from dodeal_ai.units.call_intelligence.passes import (
    LOSS_CATEGORIES,
    NO_REASON_GIVEN,
    Extraction,
    check_extraction,
    extract,
    extraction_quotes,
    settled,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.wave2_answers import coaching_answer
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
        language="en",
        confidence=0.9,
    )


# An invented call that dies: the client is not interested and says no more.
SEGMENTS = (
    _say(0, "agent", "Good morning, this is the sales office about the villa."),
    _say(5, "lead", "Sorry, I am not interested. Please do not call again."),
    _say(10, "agent", "Understood, the price is high for many clients."),
    _say(15, "lead", "Thank you, goodbye."),
)


def _call() -> CallText:
    return CallText.of(
        Transcript.of(SEGMENTS, provider="fake", model="fake"), country_code="971"
    )


NO_STEP = {
    "action": None,
    "owner": "unknown",
    "due": None,
    "quote": None,
    "segment": None,
}


def _dead(loss_reason: dict[str, Any] | None, ending: str = "dead") -> dict:
    answer = _extraction()
    answer.update(
        wanted=None,
        agreed=[],
        next_step=dict(NO_STEP),
        ending=ending,
        mood={"value": "negative", "quote": "I am not interested", "segment": "s2"},
        loss_reason=loss_reason,
    )
    return answer


def _lost(category: str, quote: str | None = None, segment: str | None = None):
    return {"category": category, "quote": quote, "segment": segment}


def _kept(answer: dict[str, Any]) -> Any:
    return settled(Extraction.model_validate(answer), _call())["loss_reason"]


# --- the guard ---------------------------------------------------------------------


async def test_not_interested_with_no_reason_is_no_reason_given_and_ask_why() -> None:
    """The guard: "not interested" and nothing more is no_reason_given, kept
    with the client's quote; and the coaching then suggests asking why."""
    lost = _lost(NO_REASON_GIVEN, "I am not interested", "s2")
    llm = FakeLLM(json_response(_dead(lost)))

    found, _ = await extract(llm, _call(), scope=SCOPE, settings=get_settings())

    assert llm.call_count == 1
    assert settled(found, _call())["loss_reason"] == {**lost, "unverified": False}
    coaching = Coaching.model_validate(coaching_answer(SEGMENTS[0].text))
    part = coaching_part(_call(), coaching, NO_REASON_GIVEN)
    assert part["ask_why"] == ASK_WHY["en"]
    assert coaching_part(_call(), coaching, "price")["ask_why"] is None
    assert coaching_part(_call(), coaching)["ask_why"] is None


# --- the rules ---------------------------------------------------------------------


def test_the_categories_are_the_nine_objections_and_no_reason_given() -> None:
    assert LOSS_CATEGORIES == (*OBJECTION_CATEGORIES, NO_REASON_GIVEN)
    category = passes.LossReason.model_fields["category"].annotation
    assert get_args(category.__value__) == LOSS_CATEGORIES


def test_no_reason_given_may_come_without_a_quote() -> None:
    answer = _dead(_lost(NO_REASON_GIVEN))
    check_extraction(_call())(Extraction.model_validate(answer))
    assert _kept(answer) == {**_lost(NO_REASON_GIVEN), "unverified": False}


@pytest.mark.parametrize(
    ("lost", "error"),
    [
        (_lost("price"), "quote_missing"),
        (_lost("price", "the price is high", "s3"), "quote_wrong_speaker"),
        (_lost("price", "it is far too expensive", "s2"), "quote_not_in_segment"),
        (_lost(NO_REASON_GIVEN, "the price is high", "s3"), "quote_wrong_speaker"),
    ],
    ids=["unquoted", "agents-words", "invented", "no-reason-agents-words"],
)
def test_a_reason_is_the_clients_own_quote_or_kept_unverified(
    lost: dict[str, Any], error: str
) -> None:
    """A category is owed the client's quote; one that fails is kept, its
    quote removed and unverified true, as an element's is."""
    answer = _dead(lost)
    check_extraction(_call())(Extraction.model_validate(answer))
    found = extraction_quotes(_call(), Extraction.model_validate(answer))
    assert found["loss_reason"] == [("loss_reason", error)]
    kept = _kept(answer)
    assert (kept["category"], kept["quote"], kept["unverified"]) == (
        lost["category"],
        None,
        True,
    )


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (_dead(None), "dead_without_loss_reason"),
        (_dead(_lost(NO_REASON_GIVEN), ending="stalled"), "loss_reason_without_dead"),
    ],
    ids=["dead-no-reason", "reason-not-dead"],
)
def test_a_loss_reason_goes_with_a_dead_ending_and_only_with_one(
    answer: dict[str, Any], error: str
) -> None:
    with pytest.raises(OutputValidationError) as refused:
        check_extraction(_call())(Extraction.model_validate(answer))
    assert refused.value.errors == (("loss_reason", error),)


def test_a_call_not_dead_has_no_loss_reason() -> None:
    assert (
        settled(Extraction.model_validate(_extraction()), _call())["loss_reason"]
        is None
    )


def test_a_category_off_the_list_is_refused_by_the_schema() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Extraction.model_validate(_dead(_lost("bored")))


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_tip_is_in_its_language_and_passes_the_tone_list(language: str) -> None:
    tip = ASK_WHY[language]
    assert in_language(tip, language)
    assert not harsh(tip)
