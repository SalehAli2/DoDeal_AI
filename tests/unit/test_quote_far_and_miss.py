"""D-102: a quote the model cited a few segments off is found where it was
said, and every quote still refused is logged as quote_miss with why --
elsewhere, joined, altered_N, absent, unchecked -- never its words."""

from __future__ import annotations

import logging

import pytest

from dodeal_ai.units.call_intelligence.coaching import (
    COACHING_LABEL,
    Coaching,
    refused_coaching,
)
from dodeal_ai.units.call_intelligence.evidence import (
    FAR_MIN_WORDS,
    CallText,
    found_at,
    locate,
    log_misses,
    miss_kind,
    quote_at,
    quote_errors,
)
from dodeal_ai.units.call_intelligence.score import (
    SCORE_LABEL,
    ScoreChecks,
    merged_checks,
    refused_checks,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from tests.unit.test_call_coaching import ANSWER as COACHING_ANSWER
from tests.unit.test_call_score import _checks


def _say(start: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


SEGMENTS = (
    _say(0, "agent", "Good morning, what budget do you have in mind?"),
    _say(5, "lead", "Around two million, for my family to live in."),
    _say(10, "agent", "Shall I book a viewing on Tuesday at four?"),
    _say(15, "lead", "Yes, Tuesday at four works for me."),
    _say(20, "agent", "Great, I will send the brochure tonight."),
    _say(25, "lead", "Thank you, speak soon."),
    _say(30, "agent", "Thank you, what budget do you have in mind again?"),
)


def _call() -> CallText:
    return CallText.of(
        Transcript.of(SEGMENTS, provider="f", model="f"), country_code="971"
    )


# --- the far search ---------------------------------------------------------------


def test_four_words_cited_far_off_are_found_where_they_were_said() -> None:
    assert FAR_MIN_WORDS == 4
    assert locate(_call(), "book a viewing on Tuesday", "s6") == "s3"
    assert quote_errors(_call(), "q", "book a viewing on Tuesday", "s6") == []


def test_three_words_cited_far_off_are_still_refused() -> None:
    """Short phrases recur; three words are looked for next door only."""
    assert found_at(_call(), "book a viewing", "s6") is None
    assert quote_errors(_call(), "q", "book a viewing", "s6") == [
        ("q", "quote_not_in_segment")
    ]


def test_the_nearest_of_two_places_is_taken() -> None:
    """The phrase is said in s1 and s7; cited at s5 it is s7, two off, not
    s1, four off."""
    assert locate(_call(), "what budget do you have", "s5") == "s7"
    assert locate(_call(), "what budget do you have", "s3") == "s1"


def test_the_far_segment_is_still_held_to_its_speaker() -> None:
    assert quote_errors(
        _call(), "q", "book a viewing on Tuesday", "s6", speaker="client"
    ) == [("q", "quote_wrong_speaker")]


def test_a_dropped_negation_is_found_nowhere_near_or_far() -> None:
    said = (_say(0, "lead", "I do not want to wait."), *SEGMENTS[1:])
    call = CallText.of(Transcript.of(said, provider="f", model="f"), country_code="971")
    assert found_at(call, "I do want to wait", "s6") is None


# --- why a quote missed -----------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "segment", "kind"),
    [
        ("works for me", "s7", "elsewhere"),
        ("Tuesday at four? Yes, Tuesday", "s3", "joined"),
        ("Shall I book a visit on Tuesday at four", "s3", "altered_1"),
        ("Shall I Tuesday at four", "s3", "spread"),
        ("Yes Tuesday works for me", "s4", "spread"),
        ("Shall we book a visit on Wednesday at four", "s3", "altered_3"),
        ("the client wanted a sea view flat", "s3", "absent"),
        ("book a viewing", None, "unchecked"),
        ("book a viewing", "s99", "unchecked"),
    ],
)
def test_the_miss_kind(quote: str, segment: str | None, kind: str) -> None:
    assert miss_kind(_call(), quote, segment) == kind


def test_a_negation_in_the_gap_is_named() -> None:
    said = (_say(0, "lead", "I do not want to wait for the handover."),)
    call = CallText.of(Transcript.of(said, provider="f", model="f"), country_code="971")
    assert miss_kind(call, "I do want to wait", "s1") == "negation"


def test_a_miss_is_logged_by_where_and_why_never_its_words(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard: one quote_miss line per quote check failure -- the label,
    the path, the kind (miss_kind's, or the failure's own code, D-105) and
    the word count -- not one word of the quote, and nothing for a failure
    that is not a quote's."""
    quotes = {
        "courteous": ("Shall I book a visit on Tuesday at four", "s3"),
        "asked_budget": (None, None),
        "observations": (None, None),
    }
    errors = [
        ("courteous", "quote_not_in_segment"),
        ("asked_budget", "quote_missing"),
        ("observations", "no_strength"),
    ]
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.unit_b"):
        log_misses(SCORE_LABEL, _call(), errors, quotes.__getitem__)
    assert [r.getMessage() for r in caplog.records] == [
        "quote_miss label=llm.unit_b.score where=courteous kind=altered_1 words=9",
        "quote_miss label=llm.unit_b.score where=asked_budget kind=quote_missing words=0",
    ]
    assert "visit" not in caplog.text


def test_quote_at_reads_a_quote_by_the_path_the_checks_name_it() -> None:
    """D-105: one resolver for every pass's paths -- a field, a list index, a
    said, and a name whose <name>_quote the model before it holds."""
    from dodeal_ai.units.call_intelligence.passes import Extraction
    from tests.unit.test_call_next_step import _booking

    answer = Extraction.model_validate(_booking("s56", "s57"))
    at = quote_at(answer)
    assert at("next_step") == ("اوكي", "s57")
    assert at("next_step.when") == ("بكرة الساعة 5", "s56")
    assert at("details.budget") == (
        answer.details.budget.quote,
        answer.details.budget.segment,
    )
    assert at("agreed.9") == (None, None)
    assert at("nowhere.at.all") == (None, None)
    assert at("observations") == (None, None)
    assert (at("details"), at("agreed")) == ((None, None), (None, None))


def test_the_extraction_logs_its_misses_kept_or_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-105: the extraction's failing quotes are logged as the score's and
    coaching's are, whether the answer is kept or refused."""
    from dodeal_ai.units.call_intelligence.passes import (
        EXTRACT_LABEL,
        Extraction,
        check_extraction,
    )
    from tests.unit.test_call_next_step import _booked, _booking, _turns

    call = _turns(s56=("agent", "تمام، بكلمك بكرة الساعة 5"), s57=("lead", "اوكي"))
    assert _booked(call, _booking("s56", "s57"))["booked"] is True
    answer = Extraction.model_validate(_booking("s56", "s57", "موافق"))
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.unit_b"):
        check_extraction(call)(answer)
    assert [r.getMessage() for r in caplog.records] == [
        f"quote_miss label={EXTRACT_LABEL} where=next_step kind=absent words=1"
    ]


def test_the_score_logs_the_first_answers_misses_and_the_seconds_left(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = ScoreChecks.model_validate(
        _checks(courteous="Good morning to you", asked_purpose="to live in forever")
    )
    second = ScoreChecks.model_validate(
        _checks(courteous="Good morning", asked_purpose="for my family to stay")
    )
    call = CallText.of(
        Transcript.of(SEGMENTS[:2], provider="f", model="f"), country_code="971"
    )
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.unit_b"):
        errors = refused_checks(call)(first)
        merged = merged_checks(call)(first, second)
    assert [where for where, _ in errors] == ["asked_purpose", "courteous"]
    assert merged.courteous == second.courteous
    assert [r.getMessage() for r in caplog.records] == [
        "quote_miss label=llm.unit_b.score where=asked_purpose kind=altered_1 words=4",
        "quote_miss label=llm.unit_b.score where=courteous kind=altered_1 words=4",
        "quote_miss label=llm.unit_b.score where=asked_purpose kind=altered_1 words=5",
    ]


def test_coaching_logs_its_observation_and_stage_misses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.unit.test_call_coaching import _answer, _stage
    from tests.unit.test_call_coaching import _call as coaching_call

    answer = _answer()
    answer["observations"][0]["quote"] = "thank you for taking my call now"
    answer["stages"]["close"] = _stage("yes", "we will book the viewing", "s2")
    for name in ("rapport", "presentation", "objections"):
        answer["stages"][name] = _stage("yes", "said nowhere on this call", "s2")
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.unit_b"):
        refused_coaching(coaching_call())(Coaching.model_validate(answer))
    wheres = [r.getMessage().split(" ")[2] for r in caplog.records]
    assert wheres[0] == "where=observations.0"
    assert "where=stages.close" in wheres
    assert all(
        r.getMessage().startswith(f"quote_miss label={COACHING_LABEL}")
        for r in caplog.records
    )
    assert "taking my call now" not in caplog.text
    assert COACHING_ANSWER["stages"]["close"]["quote"] is None
