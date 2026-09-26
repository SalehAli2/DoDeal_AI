"""Wave 1's passes (units/call_intelligence/passes.py) on the FakeLLM: the quote
check in code, one reprompt with Unit B's tail, a not-mentioned detail held
null, certainty decided in code, and the prose's length and language."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import MalformedOutputError
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_EXTRACT, PROFILE_UNIT_B_PROSE
from dodeal_ai.core.prompting import build_prompt
from dodeal_ai.units.call_intelligence import passes
from dodeal_ai.units.call_intelligence.evidence import quote_errors
from dodeal_ai.units.call_intelligence.passes import (
    NOT_MENTIONED,
    STATED,
    UNCERTAIN,
    CallText,
    extract,
    mood_uncertain,
    settled,
    write_prose,
)
from dodeal_ai.units.call_intelligence.prompts import (
    EXTRACT_TEMPLATE,
    QUOTE_EXACT_TAIL_TEMPLATE,
    QUOTE_LENGTH_TAIL_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    build_call_prompt,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.structured_intelligence import llm_call
from tests.conftest import RedisFakes
from tests.helpers.fake_llm import FakeLLM, json_response

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")


def _segment(start: float, speaker: str, text: str, confidence: float = 0.9):
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=confidence,
    )


# An invented call.
SEGMENTS = (
    _segment(0, "agent", "Good morning, this is the sales office about the villa."),
    _segment(5, "lead", "My budget is 1,200,000 AED, call me on 050 123 4567."),
    _segment(10, "agent", "Shall we book the viewing on Tuesday at four?"),
    _segment(15, "lead", "Yes, Tuesday works. I am happy with that."),
)


def _call(*segments: Segment) -> CallText:
    transcript = Transcript.of(segments or SEGMENTS, provider="fake", model="fake")
    return CallText.of(transcript, country_code="971")


def _detail(
    value: str | None = None,
    state: str = NOT_MENTIONED,
    quote: str | None = None,
    segment: str | None = None,
) -> dict[str, Any]:
    return {"value": value, "state": state, "quote": quote, "segment": segment}


def _said(text: str, quote: str, segment: str) -> dict[str, str]:
    return {"text": text, "quote": quote, "segment": segment}


def _extraction(**details: dict[str, Any]) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "wanted": _said("A villa within budget.", "My budget is 1,200,000 AED", "s2"),
        "discussed": ["budget", "viewing"],
        "concerns": [],
        "agreed": [_said("a viewing on Tuesday", "Yes, Tuesday works", "s4")],
        "next_step": {
            "action": "Viewing",
            "owner": "agent",
            "due": "Tuesday at four",
            "quote": "Shall we book the viewing on Tuesday at four?",
            "segment": "s3",
        },
        "ending": "moved_forward",
        "details": {name: _detail() for name in passes.DETAIL_NAMES},
        "mood": {"value": "positive", "quote": "I am happy with that", "segment": "s4"},
    }
    answer["details"].update(details)
    return answer


BUDGET = _detail("1,200,000 AED", STATED, "my budget is 1,200,000 AED", "s2")
PROSE = {
    "summary": "The client wants a villa. A viewing is booked for Tuesday.",
    "crm_note": "Villa, budget 1,200,000 AED. Viewing Tuesday at four.",
}


async def _extract(llm: FakeLLM, call: CallText | None = None):
    return await extract(llm, call or _call(), scope=SCOPE, settings=get_settings())


# --- the guard -------------------------------------------------------------------


async def test_an_invented_quote_reprompts_then_fails_the_pass() -> None:
    invented = _detail("2,000,000 AED", STATED, "I can pay two million", "s2")
    answer = json_response(_extraction(budget=invented))
    llm = FakeLLM(answer, answer)

    with pytest.raises(MalformedOutputError):
        await _extract(llm)

    assert llm.call_count == 2
    tail = build_prompt(QUOTE_EXACT_TAIL_TEMPLATE, caller_data="").stable
    assert (llm.prompts[0].tail, llm.prompts[1].tail) == ("", tail)
    assert llm.prompts[1].variable == llm.prompts[0].variable


def _tail(template: str) -> str:
    return build_prompt(template, caller_data="").stable


@pytest.mark.parametrize(
    ("budget", "template"),
    [
        (_detail("x", STATED, "budget " * 41, "s2"), QUOTE_LENGTH_TAIL_TEMPLATE),
        (
            _detail("x", STATED, "I can pay two million", "s2"),
            QUOTE_EXACT_TAIL_TEMPLATE,
        ),
        (_detail("x", STATED, None, None), REPROMPT_TAIL_TEMPLATE),
    ],
)
async def test_each_quote_code_from_the_extract_pass_picks_its_tail(
    budget: dict, template: str
) -> None:
    """The guard: a quote_length failure reprompts with the quote tail, a quote
    not in its segment with the exact-copy tail, anything else with the usual."""
    rejected = _extraction(budget=budget)
    llm = FakeLLM(json_response(rejected), json_response(_extraction()))

    await _extract(llm)

    first, second = llm.prompts
    assert (first.tail, second.tail) == ("", _tail(template))
    assert (second.stable, second.variable) == (first.stable, first.variable)
    assert "budget budget" not in second.tail + second.variable


def _rejects_once(*errors: tuple[str, str]):
    """A check that refuses the first answer with `errors`, then passes."""
    from dodeal_ai.core.validation import OutputValidationError

    calls: list[int] = []

    def check(_answer: passes.Extraction) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OutputValidationError(passes.EXTRACT_LABEL, errors)

    return check


async def _reprompted_tail(check) -> str:
    """The tail call_model sends on the reprompt after `check` refuses."""
    llm = FakeLLM(json_response(_extraction()), json_response(_extraction()))
    await llm_call.call_model(
        llm,
        build_call_prompt(EXTRACT_TEMPLATE, _call().data()),
        passes.Extraction,
        passes.EXTRACT_LABEL,
        scope=SCOPE,
        settings=get_settings(),
        profile=PROFILE_UNIT_B_EXTRACT,
        max_output_tokens=passes.EXTRACT_MAX_OUTPUT_TOKENS,
        check=check,
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=REPROMPT_TAILS,
    )
    return llm.prompts[1].tail


@pytest.mark.parametrize(
    ("errors", "template"),
    [
        (
            (("details.x", "missing_field"), ("details.budget", "quote_length")),
            QUOTE_LENGTH_TAIL_TEMPLATE,
        ),
        (
            (("wanted", "missing_field"), ("agreed.0", "quote_not_in_segment")),
            QUOTE_EXACT_TAIL_TEMPLATE,
        ),
        (
            (("agreed.0", "quote_not_in_segment"), ("wanted", "quote_length")),
            QUOTE_LENGTH_TAIL_TEMPLATE,
        ),
        (
            (("wanted", "missing_field"), ("agreed.0", "segment_unknown")),
            REPROMPT_TAIL_TEMPLATE,
        ),
    ],
)
async def test_a_quote_code_anywhere_in_the_errors_picks_the_quote_tail(
    errors: tuple[tuple[str, str], ...], template: str
) -> None:
    """The guard: [missing_field, quote_length] gets the quote-length tail;
    quote_length wins over quote_not_in_segment wherever each stands."""
    assert await _reprompted_tail(_rejects_once(*errors)) == _tail(template)


async def test_call_model_without_a_tail_map_keeps_its_one_tail() -> None:
    """Unit A's default: a quote_length failure still gets reprompt_tail."""
    rejected = _extraction(budget=_detail("x", STATED, "budget " * 41, "s2"))
    llm = FakeLLM(json_response(rejected), json_response(_extraction()))

    await llm_call.call_model(
        llm,
        build_call_prompt(EXTRACT_TEMPLATE, _call().data()),
        passes.Extraction,
        passes.EXTRACT_LABEL,
        scope=SCOPE,
        settings=get_settings(),
        profile=PROFILE_UNIT_B_EXTRACT,
        max_output_tokens=passes.EXTRACT_MAX_OUTPUT_TOKENS,
        check=passes.check_extraction(_call()),
    )

    assert llm.prompts[1].tail == _tail(llm_call.REPROMPT_TAIL_TEMPLATE)


async def test_a_not_mentioned_budget_stays_null() -> None:
    llm = FakeLLM(json_response(_extraction()))
    answer, _ = await _extract(llm)

    budget = settled(answer, _call()).details.budget
    assert (budget.value, budget.state, budget.quote) == (None, NOT_MENTIONED, None)


async def test_a_not_mentioned_detail_with_a_value_is_malformed() -> None:
    guessed = _detail("1,000,000 AED", NOT_MENTIONED)
    llm = FakeLLM(json_response(_extraction(budget=guessed)))
    llm.script(json_response(_extraction()))

    answer, _ = await _extract(llm)

    assert llm.call_count == 2 and answer.details.budget.value is None


# --- the quote check -------------------------------------------------------------


async def test_a_true_quote_passes_whatever_its_case_and_punctuation() -> None:
    llm = FakeLLM(json_response(_extraction(budget=BUDGET)))
    answer, response = await _extract(llm)
    assert answer.details.budget.state == STATED
    assert llm.call_count == 1 and response is not None


@pytest.mark.parametrize(
    ("budget", "error"),
    [
        (_detail("x", STATED, "My budget", None), "quote_without_segment"),
        (_detail("x", STATED, None, None), "stated_without_quote"),
        (_detail("x", STATED, "My budget", "s9"), "segment_unknown"),
        (_detail("x", STATED, "budget " * 41, "s2"), "quote_length"),
        (_detail("x", UNCERTAIN, "...", "s2"), "quote_length"),
        (_detail("x", STATED, "budget is", "s4"), "quote_not_in_segment"),
        (_detail("x", STATED, "call me on 050 123 4567", "s2"), "quote_not_in_segment"),
    ],
)
def test_every_broken_quote_is_malformed(budget: dict, error: str) -> None:
    from dodeal_ai.core.validation import OutputValidationError

    answer = passes.Extraction.model_validate(_extraction(budget=budget))
    with pytest.raises(OutputValidationError) as refused:
        passes.check_extraction(_call())(answer)
    assert ("details.budget", error) in refused.value.errors


# One long segment of 41 distinct words, so a quote of its first n is exact.
_LONG = " ".join(f"word{n}" for n in range(1, 42))


@pytest.mark.parametrize(("count", "errors"), [(30, []), (40, [])])
def test_an_exact_quote_of_up_to_40_words_passes(count: int, errors: list) -> None:
    """The guard: headroom above the prompts' 15 words keeps a long true quote."""
    call = _call(_segment(0, "lead", _LONG))
    quote = " ".join(_LONG.split()[:count])
    assert quote_errors(call, "here", quote, "s1") == errors


def test_an_exact_quote_of_41_words_is_quote_length() -> None:
    call = _call(_segment(0, "lead", _LONG))
    assert quote_errors(call, "here", _LONG, "s1") == [("here", "quote_length")]


def test_a_masked_number_may_be_quoted_as_masked() -> None:
    masked = _detail("[PHONE]", STATED, "call me on [PHONE]", "s2")
    answer = passes.Extraction.model_validate(_extraction(budget=masked))
    passes.check_extraction(_call())(answer)


def _refused(answer: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    from dodeal_ai.core.validation import OutputValidationError

    with pytest.raises(OutputValidationError) as refused:
        passes.check_extraction(_call())(passes.Extraction.model_validate(answer))
    return refused.value.errors


async def test_an_agreed_item_without_a_real_quote_reprompts_then_fails() -> None:
    """The guard: every element's evidence goes through the quote check."""
    answer = _extraction()
    answer["agreed"] = [_said("a discount of ten percent", "I give you 10%", "s3")]
    llm = FakeLLM(json_response(answer), json_response(answer))

    with pytest.raises(MalformedOutputError):
        await _extract(llm)
    assert llm.call_count == 2
    assert _refused(answer) == (("agreed.0", "quote_not_in_segment"),)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            {"concerns": [_said("price", "too expensive", "s2")]},
            ("concerns.0", "quote_not_in_segment"),
        ),
        (
            {"wanted": _said("A villa.", "I want a penthouse", "s2")},
            ("wanted", "quote_not_in_segment"),
        ),
        (
            {"agreed": [_said("a viewing", "Yes, Tuesday works", "s9")]},
            ("agreed.0", "segment_unknown"),
        ),
    ],
    ids=["concern", "wanted", "agreed-segment"],
)
def test_every_elements_evidence_is_quote_checked(
    change: dict[str, Any], error: tuple[str, str]
) -> None:
    assert _refused({**_extraction(), **change}) == (error,)


@pytest.mark.parametrize(
    ("quote", "segment", "error"),
    [
        (None, None, "quote_missing"),
        ("Shall we book", None, "quote_without_segment"),
        ("We will sign tomorrow", "s3", "quote_not_in_segment"),
    ],
)
def test_a_next_step_with_an_action_needs_its_quote(
    quote: str | None, segment: str | None, error: str
) -> None:
    answer = _extraction()
    answer["next_step"].update(quote=quote, segment=segment)
    assert _refused(answer) == (("next_step", error),)


def test_no_next_action_needs_no_quote_but_a_given_one_is_checked() -> None:
    answer = _extraction()
    answer["next_step"] = {
        "action": None,
        "owner": "unknown",
        "due": None,
        "quote": None,
        "segment": None,
    }
    answer["wanted"] = None
    passes.check_extraction(_call())(passes.Extraction.model_validate(answer))
    answer["next_step"].update(quote="nothing was said", segment="s1")
    assert _refused(answer) == (("next_step", "quote_not_in_segment"),)


@pytest.mark.parametrize(
    "item",
    [
        {"text": "a viewing", "quote": None, "segment": "s4"},
        {"text": "a viewing", "quote": "Yes, Tuesday works", "segment": None},
        "a viewing on Tuesday",
    ],
    ids=["no-quote", "no-segment", "bare-string"],
)
def test_an_agreement_without_its_evidence_is_refused_by_the_schema(
    item: object,
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        passes.Extraction.model_validate({**_extraction(), "agreed": [item]})


def test_a_mood_quote_is_checked_too() -> None:
    from dodeal_ai.core.validation import OutputValidationError

    answer = _extraction()
    answer["mood"]["quote"] = "I am thrilled"
    with pytest.raises(OutputValidationError) as refused:
        passes.check_extraction(_call())(passes.Extraction.model_validate(answer))
    assert refused.value.errors == (("mood", "quote_not_in_segment"),)


# --- certainty, in code ----------------------------------------------------------


async def test_a_detail_citing_a_low_confidence_segment_is_uncertain() -> None:
    doubtful = (
        *SEGMENTS[:1],
        _segment(5, "lead", SEGMENTS[1].text, 0.4),
        *SEGMENTS[2:],
    )
    call = _call(*doubtful)
    assert not call.uncertain
    answer, _ = await _extract(FakeLLM(json_response(_extraction(budget=BUDGET))), call)

    kept = settled(answer, call)
    assert (kept.details.budget.state, kept.details.budget.value) == (
        UNCERTAIN,
        "1,200,000 AED",
    )
    assert not mood_uncertain(answer, call)


async def test_an_uncertain_transcript_makes_every_field_uncertain() -> None:
    call = _call(*(_segment(s.start_s, s.speaker, s.text, 0.3) for s in SEGMENTS))
    assert call.uncertain
    answer, _ = await _extract(FakeLLM(json_response(_extraction(budget=BUDGET))), call)

    kept = settled(answer, call)
    details = [getattr(kept.details, name) for name in passes.DETAIL_NAMES]
    assert {detail.state for detail in details} == {UNCERTAIN}
    assert kept.details.area.value is None
    assert mood_uncertain(answer, call)


# --- what is sent ----------------------------------------------------------------


async def test_the_extract_pass_names_its_profile_ceiling_and_the_masked_copy(
    redis_fakes: RedisFakes,
) -> None:
    llm = FakeLLM(json_response(_extraction()))
    await _extract(llm)

    (sent,) = llm.calls
    assert (sent.profile, sent.max_output_tokens) == (PROFILE_UNIT_B_EXTRACT, 2500)
    assert "call me on [PHONE]" in sent.prompt.variable
    assert "123 4567" not in sent.prompt.text
    assert "1,200,000 AED" in sent.prompt.variable
    assert sent.prompt.variable.endswith("LANGUAGE: en\n----- END CALLER DATA -----")
    assert redis_fakes.cost.store["tokens:calls:tenant:tenant-a"] == 120


async def test_the_prose_pass_reads_the_settled_extraction() -> None:
    call = _call()
    extraction = passes.Extraction.model_validate(_extraction(budget=BUDGET))
    llm = FakeLLM(json_response(PROSE))

    prose, _ = await write_prose(
        llm, call, extraction, scope=SCOPE, settings=get_settings()
    )

    (sent,) = llm.calls
    assert (sent.profile, sent.max_output_tokens) == (PROFILE_UNIT_B_PROSE, 1500)
    assert '"budget": {"quote": "my budget is 1,200,000 AED"' in sent.prompt.variable
    assert sent.prompt.variable.index("TRANSCRIPT:") < sent.prompt.variable.index(
        "EXTRACTION:"
    )
    assert prose.crm_note == PROSE["crm_note"]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"crm_note": "word " * 81}, "crm_note_too_long"),
        ({"summary": "العميل يريد فيلا."}, "wrong_language"),
        ({"summary": "العميل يريد فيلا near the park."}, "wrong_language"),
        ({"crm_note": '"1,200,000"'}, "wrong_language"),
    ],
    ids=["long-note", "arabic", "arabic-mostly", "only-a-quote"],
)
async def test_a_long_note_or_the_wrong_language_is_malformed(
    change: dict, error: str
) -> None:
    from dodeal_ai.core.validation import OutputValidationError

    answer = passes.Prose.model_validate({**copy.deepcopy(PROSE), **change})
    with pytest.raises(OutputValidationError) as refused:
        passes.check_prose(_call())(answer)
    assert error in {code for _, code in refused.value.errors}


def test_an_arabic_call_wants_arabic_prose() -> None:
    arabic = tuple(
        Segment(
            start_s=s.start_s,
            end_s=s.end_s,
            speaker=s.speaker,
            text="نعم",
            language="ar",
            confidence=0.9,
        )
        for s in SEGMENTS
    )
    check = passes.check_prose(_call(*arabic))
    check(passes.Prose(summary="العميل يريد فيلا.", crm_note="فيلا، معاينة الثلاثاء."))


def _arabic_call() -> CallText:
    return _call(
        *(
            Segment(
                start_s=s.start_s,
                end_s=s.end_s,
                speaker=s.speaker,
                text="نعم",
                language="ar",
                confidence=0.9,
            )
            for s in SEGMENTS
        )
    )


def test_an_english_summary_with_one_arabic_name_fails_the_arabic_check() -> None:
    """The guard: one Arabic word no longer passes for an Arabic summary."""
    from dodeal_ai.core.validation import OutputValidationError

    english = "The client محمد wants a villa near the park and a viewing on Tuesday."
    answer = passes.Prose(summary=english, crm_note="فيلا، معاينة الثلاثاء.")
    with pytest.raises(OutputValidationError) as refused:
        passes.check_prose(_arabic_call())(answer)
    assert refused.value.errors == (("summary", "wrong_language"),)


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("ar", 'قال العميل "I want a villa near the park with a garden" وهو جاد.'),
        ("en", "The client said «أريد فيلا قريبة من الحديقة» and means it."),
        ("en", "The client, محمد, wants a villa near the park on Tuesday."),
    ],
    ids=["ar-quoting-english", "en-quoting-arabic", "en-one-arabic-name"],
)
def test_words_inside_quotes_do_not_count_and_one_name_does_not_fail_it(
    language: str, text: str
) -> None:
    from dodeal_ai.units.call_intelligence.evidence import in_language

    assert in_language(text, language)  # type: ignore[arg-type]
