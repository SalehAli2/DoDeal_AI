"""unit_b.extras (extras.py): keywords checked in their segment, the three tags,
the agent's dialect quoted from the agent, a WhatsApp suggestion of at most 60
words in the summary language, in the dialect code names, and never sent by
us, and seriousness read in code from stage 1's analysis and the talk, banded
in code and marked manager_only (D-105)."""

from __future__ import annotations

import ast
import copy
import logging
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
    EXTRAS_LABEL,
    NOT_HEARD,
    SERIOUSNESS_CHECKS,
    Extras,
    Facts,
    Heard,
    check_extras,
    extras_data,
    extras_part,
    extras_quotes,
    facts_of,
    find_extras,
    seriousness_band,
    seriousness_part,
)
from dodeal_ai.units.call_intelligence.language import Spoken
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    EXTRAS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
    WHATSAPP_TAIL_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import EXTRAS, wave2
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.reprompt_tails import tail_with
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
}
# The same answer as extras_v8 gave it, with the five checks it asked for.
V8_ANSWER: dict[str, Any] = {
    **ANSWER,
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


# An invented Arabic call: the agent speaks Gulf Arabic, the client answers.
AGENT_WORDS = "نبي نرتب معاينة يوم السبت"
CLIENT_WORDS = "السبت يناسبني إن شاء الله"
AR_SEGMENTS = (
    _say(0, "agent", f"هلا، شلونك؟ {AGENT_WORDS}", "ar"),
    _say(5, "lead", f"هلا، {CLIENT_WORDS}", "ar"),
)
AR_WHATSAPP = "شكرا لوقتك، نشوفك يوم السبت."
DEFAULTS = ["gulf_ar", "egyptian_ar", "levantine_ar", "iraqi_ar"]


def _dialect(
    dialect: str = "unknown", quote: str | None = None, segment: str | None = None
) -> dict[str, Any]:
    return {"dialect": dialect, "quote": quote, "segment": segment}


def _arabic(agent_dialect: dict[str, Any]) -> dict[str, Any]:
    """An answer to the Arabic call that quotes nothing but the dialect."""
    return _answer(keywords=[], whatsapp=AR_WHATSAPP, agent_dialect=agent_dialect)


# What stage 1's analysis heard on the invented call, each on its own quote.
HEARD = {
    "budget_stated": Heard(True, True, "My budget is about two million", "s2"),
    "timeline_stated": Heard(True, True, "I move in March", "s2"),
    "decision_maker_named": Heard(True, True, "My wife decides with me", "s3"),
    "next_step_agreed": Heard(True, True, "Saturday works for us", "s5"),
}


def _facts(count: int) -> Facts:
    """Facts with the first `count` of the four heard and verified."""
    return Facts(
        **{
            name: heard if n < count else NOT_HEARD
            for n, (name, heard) in enumerate(HEARD.items())
        }
    )


# --- the guard: the band table and the 60-word cap -----------------------------------


@pytest.mark.parametrize(
    ("heard", "engaged", "band"),
    [(0, False, "C"), (1, False, "C"), (1, True, "B"), (3, False, "B")]
    + [(3, True, "A"), (4, False, "A"), (4, True, "A")],
)
def test_the_band_is_computed_in_code_from_the_yes_count(
    heard: int, engaged: bool, band: str
) -> None:
    answer = Extras.model_validate(ANSWER)
    check_extras(_call())(answer)
    part = extras_part(_call(), answer, facts=_facts(heard), engaged=engaged)
    seriousness = part["seriousness"]
    yes = heard + engaged
    assert (seriousness["band"], seriousness["yes"]) == (band, yes)
    assert seriousness_band(yes) == band
    assert part["seriousness_reason"] is None


def test_a_whatsapp_suggestion_over_60_words_is_malformed() -> None:
    sixty = " ".join(["word"] * 60)
    check_extras(_call())(Extras.model_validate(_answer(whatsapp=sixty)))
    assert _refused(_answer(whatsapp=f"{sixty} more")) == (("whatsapp", "too_long"),)


async def test_a_70_word_message_leaves_keywords_tags_and_seriousness() -> None:
    """The guard: a message too long is reprompted once with the WhatsApp
    tail; too long again, it is null with its reason and the rest of the
    extras are delivered."""
    long = _answer(whatsapp=" ".join(["word"] * 70))
    llm = FakeLLM(json_response(long), json_response(long))

    found, _ = await find_extras(llm, _call(), scope=SCOPE, settings=get_settings())

    assert llm.call_count == 2
    tail = tail_with(WHATSAPP_TAIL_TEMPLATE, "$.whatsapp: too_long")
    assert (llm.prompts[0].tail, llm.prompts[1].tail) == ("", tail)
    part = extras_part(_call(), found)
    assert (part["whatsapp_suggestion"], part["whatsapp_reason"]) == (None, "too_long")
    assert part["keywords"] == [
        {**keyword, "canonical": None} for keyword in ANSWER["keywords"]
    ]
    assert part["tags"] == ANSWER["tags"]
    assert (part["seriousness"], part["seriousness_reason"]) == (
        None,
        "analysis_unavailable",
    )


async def test_a_message_mended_by_the_reprompt_is_delivered() -> None:
    long = _answer(whatsapp=" ".join(["word"] * 70))
    llm = FakeLLM(json_response(long), json_response(ANSWER))
    found, _ = await find_extras(llm, _call(), scope=SCOPE, settings=get_settings())
    part = extras_part(_call(), found)
    assert part["whatsapp_suggestion"] == {"language": "en", "text": ANSWER["whatsapp"]}
    assert part["whatsapp_reason"] is None


async def test_a_message_in_the_wrong_script_twice_is_null_with_its_reason() -> None:
    call = _call(*AR_SEGMENTS)
    english = _arabic(_dialect())
    english["whatsapp"] = "Thank you for your time, see you on Saturday."
    llm = FakeLLM(json_response(english), json_response(english))
    found, _ = await find_extras(llm, call, scope=SCOPE, settings=get_settings())
    part = extras_part(call, found)
    assert (part["whatsapp_suggestion"], part["whatsapp_reason"]) == (
        None,
        "wrong_language",
    )
    assert llm.call_count == 2


# --- the guard: the agent's dialect and the dialect of the message --------------------


def test_an_agent_dialect_quoted_from_a_client_segment_is_malformed() -> None:
    call = _call(*AR_SEGMENTS)
    client = _arabic(_dialect("gulf_ar", CLIENT_WORDS, "s2"))
    assert _refused(client, call) == (("agent_dialect", "quote_wrong_speaker"),)
    unknown = _arabic(_dialect("unknown", CLIENT_WORDS, "s2"))
    assert _refused(unknown, call) == (("agent_dialect", "quote_wrong_speaker"),)
    check_extras(call)(
        Extras.model_validate(_arabic(_dialect("gulf_ar", AGENT_WORDS, "s1")))
    )


def test_a_known_agent_dialect_is_owed_a_true_quote() -> None:
    call = _call(*AR_SEGMENTS)
    assert _refused(_arabic(_dialect("gulf_ar")), call) == (
        ("agent_dialect", "quote_missing"),
    )
    assert _refused(_arabic(_dialect("gulf_ar", "وين الفيلا", "s1")), call) == (
        ("agent_dialect", "quote_not_in_segment"),
    )


async def test_a_client_quoted_dialect_is_reprompted_once_then_fails() -> None:
    client = json_response(_arabic(_dialect("gulf_ar", CLIENT_WORDS, "s2")))
    llm = FakeLLM(client, client)
    with pytest.raises(MalformedOutputError):
        await find_extras(
            llm, _call(*AR_SEGMENTS), scope=SCOPE, settings=get_settings()
        )
    assert llm.call_count == 2


@pytest.mark.parametrize("default", DEFAULTS)
def test_an_unknown_agent_dialect_gives_the_company_default(default: str) -> None:
    call = _call(*AR_SEGMENTS)
    answer = Extras.model_validate(_arabic(_dialect()))
    check_extras(call)(answer)
    part = extras_part(call, answer, default)
    assert (part["agent_dialect"], part["whatsapp_dialect"]) == (
        {**_dialect(), "unverified": False},
        default,
    )
    assert part["whatsapp_suggestion"] == {"language": "ar", "text": AR_WHATSAPP}


@pytest.mark.parametrize("default", DEFAULTS)
def test_a_known_agent_dialect_is_the_dialect_of_the_message(default: str) -> None:
    call = _call(*AR_SEGMENTS)
    heard = _dialect("gulf_ar", AGENT_WORDS, "s1")
    answer = Extras.model_validate(_arabic(heard))
    check_extras(call)(answer)
    part = extras_part(call, answer, default)
    assert (part["agent_dialect"], part["whatsapp_dialect"]) == (
        {**heard, "unverified": False},
        "gulf_ar",
    )


@pytest.mark.parametrize("default", DEFAULTS)
def test_an_english_call_gives_an_english_message(default: str) -> None:
    call = _call()
    assert call.language == "en"
    assert _refused(_answer(whatsapp=AR_WHATSAPP), call) == (
        ("whatsapp", "wrong_language"),
    )
    answer = Extras.model_validate(ANSWER)
    check_extras(call)(answer)
    part = extras_part(call, answer, default)
    assert part["whatsapp_dialect"] is None
    assert part["whatsapp_suggestion"] == {"language": "en", "text": ANSWER["whatsapp"]}


def test_the_prompt_names_the_default_dialect_after_the_vocabulary() -> None:
    data = extras_data(_call(), ["Palm Grove Residences"], "levantine_ar")
    assert data.endswith(
        "VOCABULARY:\n- Palm Grove Residences\n\n"
        "WHATSAPP LANGUAGE: en\nDEFAULT DIALECT: levantine_ar"
    )
    assert extras_data(_call(*AR_SEGMENTS), []).endswith(
        "VOCABULARY: none\n\nWHATSAPP LANGUAGE: ar\nDEFAULT DIALECT: gulf_ar"
    )


def test_an_answer_kept_before_the_dialect_reads_back_as_unknown() -> None:
    kept = Extras.model_validate(ANSWER)
    assert kept.agent_dialect.model_dump() == _dialect()


# --- the guard: the message in the client's language ----------------------------------

UR_WHATSAPP = "آپ کے وقت کا شکریہ، ہفتے کو ملتے ہیں۔"
RU_WHATSAPP = "Спасибо за ваше время, до встречи в субботу."


def _heard(client: str | None, agent: str | None, *segments: Segment) -> CallText:
    """The call with each side's language as the roles pass heard it."""
    transcript = Transcript.of(segments or SEGMENTS, provider="fake", model="fake")
    return CallText.of(transcript, country_code="971", spoken=Spoken(client, agent))


@pytest.mark.parametrize(
    ("client", "text"), [("ur", UR_WHATSAPP), ("ru", RU_WHATSAPP)], ids=["ur", "ru"]
)
def test_the_message_is_written_in_the_clients_language(client: str, text: str) -> None:
    call = _heard(client, "en")
    assert call.language == "en"
    assert f"WHATSAPP LANGUAGE: {client}\n" in extras_data(call, [])
    assert _refused(ANSWER, call) == (("whatsapp", "wrong_language"),)
    answer = Extras.model_validate(_answer(whatsapp=text))
    check_extras(call)(answer)
    part = extras_part(call, answer, "egyptian_ar")
    assert part["whatsapp_suggestion"] == {"language": client, "text": text}
    assert part["whatsapp_dialect"] is None


@pytest.mark.parametrize("default", DEFAULTS)
def test_an_arabic_speaking_client_gets_arabic_in_the_default_dialect(
    default: str,
) -> None:
    call = _heard("egyptian_ar", "en")
    assert call.language == "en"
    assert _refused(ANSWER, call) == (("whatsapp", "wrong_language"),)
    answer = Extras.model_validate(_answer(whatsapp=AR_WHATSAPP))
    check_extras(call)(answer)
    part = extras_part(call, answer, default)
    assert part["whatsapp_suggestion"] == {"language": "ar", "text": AR_WHATSAPP}
    assert part["whatsapp_dialect"] == default


def test_an_english_client_on_an_arabic_call_gets_english() -> None:
    call = _heard("en", "gulf_ar", *AR_SEGMENTS)
    assert call.language == "ar"
    heard = _dialect("gulf_ar", AGENT_WORDS, "s1")
    assert _refused(_arabic(heard), call) == (("whatsapp", "wrong_language"),)
    english = "Thank you for your time, see you on Saturday."
    answer = Extras.model_validate({**_arabic(heard), "whatsapp": english})
    check_extras(call)(answer)
    part = extras_part(call, answer, "iraqi_ar")
    assert part["whatsapp_suggestion"] == {"language": "en", "text": english}
    assert (part["agent_dialect"], part["whatsapp_dialect"]) == (
        {**heard, "unverified": False},
        None,
    )


def test_a_client_in_another_language_or_unheard_gets_the_summary_language() -> None:
    other = _heard("other", "en", *AR_SEGMENTS)
    assert other.language == "en"
    assert "WHATSAPP LANGUAGE: en\n" in extras_data(other, [])
    unheard = _heard(None, None, *AR_SEGMENTS)
    assert unheard.language == "ar"
    assert "WHATSAPP LANGUAGE: ar\n" in extras_data(unheard, [])
    answer = Extras.model_validate(_arabic(_dialect()))
    check_extras(unheard)(answer)
    assert extras_part(unheard, answer)["whatsapp_dialect"] == "gulf_ar"


# --- the rest of the rules --------------------------------------------------------------


def test_a_true_answer_passes_and_is_marked_manager_only() -> None:
    answer = Extras.model_validate(ANSWER)
    check_extras(_call())(answer)
    part = extras_part(_call(), answer, facts=_facts(4), engaged=True)
    assert part["keywords"] == [
        {**keyword, "canonical": None} for keyword in ANSWER["keywords"]
    ]
    assert part["tags"] == ANSWER["tags"]
    assert (part["agent_dialect"], part["whatsapp_dialect"]) == (
        {**_dialect(), "unverified": False},
        None,
    )
    assert part["whatsapp_suggestion"] == {"language": "en", "text": ANSWER["whatsapp"]}
    seriousness = part["seriousness"]
    assert (seriousness["band"], seriousness["manager_only"]) == ("A", True)
    assert seriousness["checks"] == {
        **{
            name: {
                "answer": "yes",
                "source": "analysis",
                "quote": heard.quote,
                "segment": heard.segment,
                "unverified": False,
            }
            for name, heard in HEARD.items()
        },
        "client_engaged": {
            "answer": "yes",
            "source": "code",
            "quote": None,
            "segment": None,
            "unverified": False,
        },
    }
    assert list(seriousness["checks"]) == list(SERIOUSNESS_CHECKS)


def test_a_keyword_not_said_in_its_segment_is_dropped() -> None:
    """Two of four failing is not most: each failing keyword is dropped on
    its own and the two true ones kept."""
    answer = _answer()
    answer["keywords"][0]["said"] = "Marina Heights"
    answer["keywords"][1]["segment"] = "s9"
    true = [
        {"kind": "topic", "said": "a viewing", "english": None, "segment": "s4"},
        {"kind": "topic", "said": "Saturday", "english": None, "segment": "s5"},
    ]
    answer["keywords"] += true
    found = Extras.model_validate(answer)
    check_extras(_call())(found)
    quotes = extras_quotes(_call(), found)
    assert (quotes["keywords.0"], quotes["keywords.1"]) == (
        [("keywords.0", "quote_not_in_segment")],
        [("keywords.1", "segment_unknown")],
    )
    assert extras_part(_call(), found)["keywords"] == [
        {**keyword, "canonical": None} for keyword in true
    ]


async def test_one_bad_keyword_quote_leaves_the_rest_delivered() -> None:
    """The guard: the keyword whose quote fails is dropped, the pass is not
    reprompted, and every other keyword, check, tag and message goes out."""
    answer = _answer()
    answer["keywords"][0]["said"] = "Marina Heights"
    llm = FakeLLM(json_response(answer))

    found, _ = await find_extras(llm, _call(), scope=SCOPE, settings=get_settings())

    assert llm.call_count == 1
    part = extras_part(_call(), found)
    assert part["keywords"] == [{**ANSWER["keywords"][1], "canonical": None}]
    assert part["tags"] == ANSWER["tags"]
    assert part["whatsapp_suggestion"]["text"] == ANSWER["whatsapp"]


def _mostly_invented() -> dict[str, Any]:
    """Three quotes -- three keywords -- two of them invented."""
    answer = _answer()
    answer["keywords"].append(
        {"kind": "topic", "said": "a viewing", "english": None, "segment": "s4"}
    )
    answer["keywords"][0]["said"] = "Marina Heights"
    answer["keywords"][1]["segment"] = "s9"
    return answer


async def test_more_than_half_of_the_quotes_failing_is_malformed() -> None:
    """The guard's other side: two of three failing is reprompted, then fails;
    one of three is kept field by field."""
    answer = _mostly_invented()
    assert len(extras_quotes(_call(), Extras.model_validate(answer))) == 3
    llm = FakeLLM(json_response(answer), json_response(answer))
    with pytest.raises(MalformedOutputError):
        await find_extras(llm, _call(), scope=SCOPE, settings=get_settings())
    assert llm.call_count == 2
    assert len(_refused(answer)) == 2

    answer["keywords"][1]["segment"] = "s3"
    check_extras(_call())(Extras.model_validate(answer))


def test_a_seven_word_said_is_dropped_and_five_are_a_name() -> None:
    """A keyword is a name, never a sentence: a seven-word said is dropped
    even when its words are in the segment it cites; five are kept."""
    answer = _answer()
    answer["keywords"][0]["said"] = "My budget is about two million, and"
    answer["keywords"][0]["segment"] = "s2"
    found = Extras.model_validate(answer)
    check_extras(_call())(found)
    assert extras_part(_call(), found)["keywords"] == [
        {**ANSWER["keywords"][1], "canonical": None}
    ]
    answer["keywords"][0]["said"] = "My budget is about two"
    kept = extras_part(_call(), Extras.model_validate(answer))["keywords"]
    assert [keyword["said"] for keyword in kept] == [
        "My budget is about two",
        "a garden",
    ]


async def test_a_seven_word_said_costs_no_reprompt() -> None:
    answer = _answer()
    answer["keywords"][0].update(
        said="My budget is about two million, and", segment="s2"
    )
    llm = FakeLLM(json_response(answer))

    found, _ = await find_extras(llm, _call(), scope=SCOPE, settings=get_settings())

    assert llm.call_count == 1
    assert len(extras_part(_call(), found)["keywords"]) == 1


# --- the seriousness, read from stage 1 (D-105) ---------------------------------------


def _analysis(**changes: Any) -> dict[str, Any]:
    """A stored stage-1 analysis of the invented call, as passes.settled
    keeps it: a budget and a timeline stated, no decision maker, a booked
    viewing; `changes` replaces a detail by name or the next step."""
    detail = {"value": None, "state": "not_mentioned", "quote": None, "segment": None}
    details = {
        "budget": {
            **detail,
            "value": "2,000,000",
            "state": "stated",
            "quote": "My budget is about two million",
            "segment": "s2",
            "evidence_failed": False,
        },
        "timeline": {
            **detail,
            "value": "March",
            "state": "stated",
            "quote": "I move in March",
            "segment": "s2",
            "evidence_failed": False,
        },
        "decision_maker": {**detail, "evidence_failed": False},
    }
    step = {
        "action": "Viewing on Saturday",
        "kind": "viewing",
        "quote": "Saturday works for us",
        "segment": "s5",
        "unverified": False,
        "booked": True,
    }
    for name, changed in changes.items():
        if name == "next_step":
            step = changed
        else:
            details[name] = changed
    return {"details": details, "elements": {"next_step": step}}


def test_the_facts_are_read_from_stage_1s_analysis() -> None:
    """The guard (D-105): each check is what stage 1 already judged, on the
    quote it already verified; nothing is asked of the model again."""
    facts = facts_of(_analysis())
    assert facts == Facts(
        budget_stated=HEARD["budget_stated"],
        timeline_stated=HEARD["timeline_stated"],
        decision_maker_named=NOT_HEARD,
        next_step_agreed=HEARD["next_step_agreed"],
    )
    serious = seriousness_part(facts, engaged=True)
    assert serious is not None
    assert (serious["yes"], serious["band"]) == (4, "A")


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"budget": {"state": "uncertain", "evidence_failed": True}}, True),
        ({"budget": {"state": "uncertain", "evidence_failed": False}}, True),
        ({"budget": {"state": "stated", "evidence_failed": True}}, True),
        ({"budget": {"state": "not_mentioned"}}, False),
        ({"budget": None}, False),
    ],
    ids=["quote-failed", "said-vaguely", "flagged-failed", "not-mentioned", "absent"],
)
def test_a_detail_said_but_not_verified_is_yes_and_never_counted(
    change: dict[str, Any], said: bool
) -> None:
    """A detail said but not on its own verified quote (uncertain, as
    passes.settled keeps a failed one) is a yes, unverified, and does not
    count toward the band."""
    facts = facts_of(_analysis(**change))
    assert facts is not None
    assert facts.budget_stated == Heard(said=said, verified=False)
    serious = seriousness_part(facts, engaged=False)
    assert serious is not None
    assert serious["checks"]["budget_stated"] == {
        "answer": "yes" if said else "no",
        "source": "analysis",
        "quote": None,
        "segment": None,
        "unverified": said,
    }
    assert serious["yes"] == 2


@pytest.mark.parametrize(
    ("step", "heard"),
    [
        (
            {"action": "Viewing", "quote": None, "segment": None, "unverified": True},
            Heard(said=True, verified=False),
        ),
        ({"action": None, "quote": None, "segment": None}, NOT_HEARD),
        (None, NOT_HEARD),
    ],
    ids=["quote-failed", "no-action", "absent"],
)
def test_the_next_step_counts_only_on_its_verified_quote(
    step: dict[str, Any] | None, heard: Heard
) -> None:
    facts = facts_of(_analysis(next_step=step))
    assert facts is not None and facts.next_step_agreed == heard


def test_with_no_analysis_the_seriousness_is_null_with_its_reason() -> None:
    assert facts_of(None) is None
    part = extras_part(_call(), Extras.model_validate(ANSWER), facts=None)
    assert (part["seriousness"], part["seriousness_reason"]) == (
        None,
        "analysis_unavailable",
    )


def test_an_answer_kept_under_v8_reads_back_its_seriousness_unused() -> None:
    """A re-run reads the answer it paid for: v8's five checks still parse,
    and the part is stage 1's, never theirs."""
    kept = Extras.model_validate(V8_ANSWER)
    assert kept.seriousness is not None
    part = extras_part(_call(), kept, facts=_facts(0), engaged=False)
    assert (part["seriousness"]["yes"], part["seriousness"]["band"]) == (0, "C")
    assert "seriousness" not in {w.split(".")[0] for w in extras_quotes(_call(), kept)}


def test_the_prompt_asks_nothing_about_the_client() -> None:
    from dodeal_ai.core.prompting import build_prompt

    text = build_prompt(EXTRAS_TEMPLATE, caller_data="").stable
    assert EXTRAS_TEMPLATE == "call_intelligence/extras_v9.txt"
    for asked in ("seriousness", "budget_stated", "next_step_agreed", "reason"):
        assert asked not in text


def test_every_failing_quote_is_logged_with_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-105: the extras pass logs each failing quote as quote_miss, codes
    only, kept or refused."""
    answer = _answer()
    answer["keywords"][0]["said"] = "Marina Heights"
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.unit_b"):
        check_extras(_call())(Extras.model_validate(answer))
    assert [r.getMessage() for r in caplog.records] == [
        f"quote_miss label={EXTRAS_LABEL} where=keywords.0 kind=absent words=2"
    ]
    assert "Marina" not in caplog.text


def test_an_agent_dialect_on_a_failing_quote_is_kept_unverified() -> None:
    """Beside two true quotes, a client-quoted agent dialect is kept, its
    quote removed and unverified true; the message's dialect is still the
    one it was asked in."""
    call = _call(*AR_SEGMENTS)
    answer = _arabic(_dialect("gulf_ar", CLIENT_WORDS, "s2"))
    answer["keywords"] = [
        {"kind": "topic", "said": "معاينة", "english": "viewing", "segment": "s1"},
        {"kind": "topic", "said": "السبت", "english": "Saturday", "segment": "s2"},
    ]
    found = Extras.model_validate(answer)
    check_extras(call)(found)
    part = extras_part(call, found, "iraqi_ar")
    assert part["agent_dialect"] == {
        "dialect": "gulf_ar",
        "quote": None,
        "segment": None,
        "unverified": True,
    }
    assert part["whatsapp_dialect"] == "gulf_ar"


def test_a_whatsapp_suggestion_in_the_wrong_language_is_malformed() -> None:
    arabic = tuple(_say(s.start_s, s.speaker, s.text, "ar") for s in SEGMENTS)
    call = _call(*arabic)
    assert call.language == "ar"
    answer = _answer(keywords=[])
    assert _refused(answer, call) == (("whatsapp", "wrong_language"),)
    arabic_text = _answer(keywords=[], whatsapp="شكرا لوقتك، نراك يوم السبت.")
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
    """LLM06: the service's only POSTs are the signed callback, the model call
    and the audio to an HTTP STT engine; extras imports nothing that sends."""
    posts = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if re.search(r"(?<!router)\.post\(", path.read_text(encoding="utf-8"))
    )
    assert posts == [
        "core/callbacks.py",
        "core/llm/openai_compatible.py",
        "units/call_intelligence/http_stt.py",
    ]
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


async def _wave2(*extras_answers: Any, config: CallsConfig | None = None):
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
    llm.script_for(
        ESCALATIONS_TEMPLATE, json_response({"claims": [], "escalations": []})
    )
    llm.script_for(COACHING_TEMPLATE, json_response(coaching_answer(SEGMENTS[0].text)))
    llm.script_for(EXTRAS_TEMPLATE, *extras_answers)
    wave = await wave2(
        llm,
        await read_job("tenant-a", "job-1"),
        config or CallsConfig(),
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


async def test_wave2_sends_the_tenant_default_dialect_to_the_extras_pass() -> None:
    config = CallsConfig(whatsapp_default_dialect="egyptian_ar")
    llm, wave = await _wave2(json_response(ANSWER), config=config)
    assert "\nDEFAULT DIALECT: egyptian_ar\n" in llm.calls[-1].prompt.variable
    part = wave.parts[EXTRAS]
    assert part is not None
    assert (part["agent_dialect"], part["whatsapp_dialect"]) == (
        {**_dialect(), "unverified": False},
        None,
    )
