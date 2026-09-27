"""unit_b.escalations (escalations.py): the BRD issues, each with a real quote
-- the agent's own for those only an agent commits -- merged by time with
stage 1's off_channel_contact, a price claim going out to be verified; and
the agent's claims, from which code alone decides an over-promise (D-104)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.jobs import JobStatus, Stage2State, create_job, read_job, transition
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_ESCALATIONS
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.escalations import (
    CERTAIN_NEGATION_WINDOW,
    MAX_CLAIMS,
    MAX_FLAGS,
    MAX_PRICE_FLAGS,
    MAX_PROMISE_FLAGS,
    Claim,
    Flags,
    escalations_part,
    find_flags,
    kept_claims,
    kept_flags,
    promises,
    said_certain,
)
from dodeal_ai.units.call_intelligence.evidence import CallText, evidence_errors
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    ESCALATIONS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import ESCALATIONS, wave2
from tests.helpers.fake_llm import FakeLLM, json_response

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


def _say(start: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


# An invented call: a guarantee, a price, and a qualified client left hanging.
SEGMENTS = (
    _say(0, "agent", "This unit will double in value in two years, guaranteed."),
    _say(5, "lead", "My budget is two million and I want to buy this year."),
    _say(10, "agent", "The price is 1,500,000 AED with no service charges."),
    _say(15, "lead", "Alright, thank you, goodbye."),
)
TRANSCRIPT = Transcript.of(SEGMENTS, provider="fake", model="fake")
OFF_CHANNEL = {
    "type": "off_channel_contact",
    "source": "alarm_phrase",
    "phrase": 0,
    "speaker": "agent",
    "start_s": 12.0,
    "segment": "s3",
}


def _call() -> CallText:
    return CallText.of(TRANSCRIPT, country_code="971")


def _flag(issue: str, quote: str, segment: str) -> dict[str, str]:
    return {"issue": issue, "quote": quote, "segment": segment}


def _claim(quote: str, segment: str, said_as: str = "certain") -> dict[str, str]:
    return {"about": "price", "said_as": said_as, "quote": quote, "segment": segment}


# The v4 habit: the model judging the promise itself. Ignored (D-104).
GUARANTEE = _flag("over_promise_or_guarantee", "will double in value", "s1")
PRICE = _flag("wrong_price_or_terms", "The price is 1,500,000 AED", "s3")
HANGING = _flag("qualified_no_next_step", "My budget is two million", "s2")
PUSHY = _flag("rudeness_or_pressure", "with no service charges", "s3")
DOUBLES = _claim("will double in value in two years", "s1")
ANSWER = {"claims": [DOUBLES], "escalations": [PRICE, GUARANTEE, HANGING]}


def _kept(*flags: dict[str, str | None]) -> list[dict[str, str | None]]:
    """The flags kept_flags keeps, in order."""
    answer = kept_flags(
        _call(), Flags.model_validate({"claims": [], "escalations": list(flags)})
    )
    return [flag.model_dump() for flag in answer.escalations]


# --- the guard ---------------------------------------------------------------------


async def test_one_bad_flag_is_dropped_and_the_rest_kept() -> None:
    """The guard (A5): a flag without a real quote is dropped on its own; the
    pass is kept with the others, on one call, and nothing is reprompted."""
    invented = _flag("rudeness_or_pressure", "sign today or lose it", "s3")
    llm = FakeLLM(
        json_response({"claims": [], "escalations": [PRICE, invented, PUSHY]})
    )

    answer, _ = await find_flags(llm, _call(), scope=SCOPE, settings=get_settings())
    assert llm.call_count == 1
    assert [flag.model_dump() for flag in answer.escalations] == [PRICE, PUSHY]
    assert _kept(invented) == []
    assert _kept({**HANGING, "quote": None, "segment": None}, HANGING) == [HANGING]


def test_flags_over_the_caps_are_dropped_by_order_never_refused() -> None:
    """A5: past MAX_FLAGS, and past MAX_PRICE_FLAGS for price claims, flags
    are dropped in the model's order; the schema takes any number."""
    prices = [
        _flag("wrong_price_or_terms", quote, "s3")
        for quote in ("The price is", "1,500,000 AED", "no service charges", "price is 1,500,000")
    ]  # fmt: skip
    assert (MAX_PRICE_FLAGS, MAX_FLAGS) == (3, 10)
    assert _kept(*prices, PUSHY) == [*prices[:3], PUSHY]
    many = [PUSHY] * 12
    assert _kept(*many) == [PUSHY] * 10


@pytest.mark.parametrize(
    ("flag", "error"),
    [
        (
            _flag("unprofessional_competitor_talk", "My budget is two million", "s2"),
            "quote_wrong_speaker",
        ),
        (_flag("rudeness_or_pressure", "guaranteed", "s9"), "segment_unknown"),
    ],
    ids=["agent-issue-from-the-client", "segment-unknown"],
)
def test_every_flag_is_quote_checked_and_an_agent_issue_is_the_agents(
    flag: dict[str, str], error: str
) -> None:
    assert evidence_errors(_call(), "x", flag["quote"], flag["segment"], speaker="agent") == [
        ("x", error)
    ]  # fmt: skip
    assert _kept(flag) == []


def test_a_qualified_client_with_no_next_step_may_quote_the_client() -> None:
    assert _kept(HANGING) == [HANGING]


def test_the_prompt_asks_for_claims_as_facts_and_a_price_as_a_term() -> None:
    from dodeal_ai.core.prompting import build_prompt

    text = " ".join(build_prompt(ESCALATIONS_TEMPLATE, caller_data="").stable.split())
    assert "WILL be or WILL do" in text
    assert "is a term, not a claim" in text
    assert "do not judge whether a claim was allowed" in text
    assert "A promise or guarantee about the future is a CLAIM" in text


# --- the part --------------------------------------------------------------------------


def test_stage1s_the_models_and_codes_escalations_merge_by_time() -> None:
    part = escalations_part(_call(), Flags.model_validate(ANSWER), [OFF_CHANNEL])
    assert [(e["type"], e["source"], e["segment"]) for e in part["items"]] == [
        ("over_promise_or_guarantee", "claim", "s1"),
        ("qualified_no_next_step", "model", "s2"),
        ("claim_to_verify", "model", "s3"),
        ("off_channel_contact", "alarm_phrase", "s3"),
    ]
    assert [(e["type"], e["segment"]) for e in part["items"]] == [
        ("over_promise_or_guarantee", "s1"),
        ("qualified_no_next_step", "s2"),
        ("claim_to_verify", "s3"),
        ("off_channel_contact", "s3"),
    ]
    claim = part["items"][2]
    assert claim == {
        "type": "claim_to_verify",
        "issue": "wrong_price_or_terms",
        "source": "model",
        "speaker": "agent",
        "start_s": 10.0,
        "segment": "s3",
        "quote": "The price is 1,500,000 AED",
    }
    assert part["items"][1]["speaker"] == "client"


def test_nothing_flagged_keeps_stage1s_alone() -> None:
    part = escalations_part(_call(), Flags(claims=[], escalations=[]), [OFF_CHANNEL])
    assert part == {"items": [OFF_CHANNEL], "claims": []}


@pytest.mark.parametrize("issue", ["price_error", "claim_to_verify"])
def test_an_issue_off_the_list_is_refused_by_the_schema(issue: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Flags.model_validate({"claims": [], "escalations": [{**PRICE, "issue": issue}]})


@pytest.mark.parametrize(
    "answer",
    [
        {"escalations": []},
        {"claims": [{**DOUBLES, "said_as": "sure"}], "escalations": []},
        {"claims": [{**DOUBLES, "about": "weather"}], "escalations": []},
    ],
    ids=["no-claims-list", "said-as-off-the-list", "about-off-the-list"],
)
def test_a_claims_list_missing_or_off_the_list_is_refused(
    answer: dict[str, Any],
) -> None:
    """Both lists are owed: a missing claims list is not "no claims", so it
    is malformed and reprompted, never read as a clean call."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Flags.model_validate(answer)


# --- the claims and the over-promise (D-104) --------------------------------------


def _part(*claims: dict[str, str], call: CallText | None = None) -> dict[str, Any]:
    answer = Flags.model_validate({"claims": list(claims), "escalations": []})
    return escalations_part(call or _call(), answer, [])


def test_a_certain_claim_is_an_over_promise_and_a_hedged_one_is_not() -> None:
    """The guard: code decides. The certain claim goes out as an
    over_promise_or_guarantee escalation, source claim; the hedged one only
    in the claims list; both with who and when read from the segment."""
    hedged = _claim("with no service charges", "s3", said_as="hedged")
    part = _part(DOUBLES, hedged)
    assert part["items"] == [
        {
            "type": "over_promise_or_guarantee",
            "issue": "over_promise_or_guarantee",
            "source": "claim",
            "about": "price",
            "speaker": "agent",
            "start_s": 0.0,
            "segment": "s1",
            "quote": "will double in value in two years",
        }
    ]
    assert [(c["said_as"], c["segment"], c["start_s"]) for c in part["claims"]] == [
        ("certain", "s1", 0.0),
        ("hedged", "s3", 10.0),
    ]


def test_the_models_own_over_promise_flag_is_ignored() -> None:
    """The v4 habit: a flag naming the promise is not a judgement code takes;
    with no claim, nothing is raised."""
    part = escalations_part(
        _call(), Flags.model_validate({"claims": [], "escalations": [GUARANTEE]}), []
    )
    assert part == {"items": [], "claims": []}


# An invented Arabic call, for the certainty words as the quote check reads
# them.
ARABIC = CallText.of(
    Transcript.of(
        (
            _say(0, "agent", "الإيجار في الحي ده دايما بيزيد كل سنة"),
            _say(5, "lead", "طيب والسعر نفسه؟"),
            _say(10, "agent", "السعر مش أكيد يزيد بس المنطقة بتتطور"),
            _say(15, "agent", "والعائد مضمون عشرة في المية من أول سنة"),
            _say(20, "lead", "الشقة دي هتغلى أكيد يعني"),
        ),
        provider="fake",
        model="fake",
    ),
    country_code="971",
)


@pytest.mark.parametrize(
    ("quote", "segment", "certain"),
    [
        ("الإيجار في الحي ده دايما بيزيد كل سنة", "s1", True),
        ("السعر مش أكيد يزيد", "s3", False),
        ("والعائد مضمون عشرة في المية", "s4", True),
    ],
    ids=["always", "not-sure-stays-hedged", "guaranteed"],
)
def test_a_certainty_word_in_the_agents_words_makes_a_claim_certain(
    quote: str, segment: str, certain: bool
) -> None:
    """The backstop: labelled hedged, a claim is still certain when its own
    words say always or guaranteed -- unless a negation comes just before."""
    claim = Claim.model_validate(_claim(quote, segment, said_as="hedged"))
    assert said_certain(claim) is certain
    part = _part(_claim(quote, segment, said_as="hedged"), call=ARABIC)
    assert len(part["items"]) == (1 if certain else 0)
    assert part["claims"][0]["said_as"] == ("certain" if certain else "hedged")


@pytest.mark.parametrize(
    "quote",
    [
        "it is not guaranteed at all",
        "the rent isn't always up",
        "not really, always",
        "it will not definitely rise",
    ],
)
def test_a_negation_just_before_a_certainty_word_keeps_the_models_label(
    quote: str,
) -> None:
    assert CERTAIN_NEGATION_WINDOW == 3
    claim = Claim.model_validate(_claim(quote, "s1", said_as="hedged"))
    assert said_certain(claim) is False
    assert said_certain(claim.model_copy(update={"said_as": "certain"})) is True


@pytest.mark.parametrize(
    "quote",
    [
        "no, this is the one, the rent is guaranteed",
        "you can always sell it for more",
        "I don't know anyone who lost, it will definitely rise",
    ],
)
def test_a_negation_further_back_or_a_contractions_stem_does_not_cancel_it(
    quote: str,
) -> None:
    """Only the three words before count, and "can't" negates by its "t":
    "can" alone is no negation."""
    claim = Claim.model_validate(_claim(quote, "s1", said_as="hedged"))
    assert said_certain(claim) is True


def test_a_clients_claim_or_a_claim_not_said_is_dropped_one_by_one() -> None:
    """The guard: a claim is the agent's own words or nothing. The client's
    "it will go up for sure, right?" is no over-promise, and an invented
    quote is dropped alone, never a reprompt."""
    clients = _claim("الشقة دي هتغلى أكيد يعني", "s5")
    invented = _claim("هتكسب الضعف مضمون", "s4")
    kept = _claim("والعائد مضمون عشرة في المية", "s4")
    part = _part(clients, invented, kept, call=ARABIC)
    assert [c["segment"] for c in part["claims"]] == ["s4"]
    assert [item["segment"] for item in part["items"]] == ["s4"]


async def test_find_flags_keeps_the_claims_found_and_relocates_them() -> None:
    """One call; a claim cited one segment off is stored where it was said,
    in the transcript's words."""
    llm = FakeLLM(
        json_response(
            {"claims": [_claim("will double in value", "s2")], "escalations": []}
        )
    )
    answer, _ = await find_flags(llm, _call(), scope=SCOPE, settings=get_settings())
    assert llm.call_count == 1
    assert [(c.segment, c.quote) for c in answer.claims] == [
        ("s1", "will double in value")
    ]


def test_the_claims_and_the_promises_are_capped_in_order() -> None:
    assert (MAX_CLAIMS, MAX_PROMISE_FLAGS) == (10, 3)
    many = Flags.model_validate({"claims": [DOUBLES] * 12, "escalations": []})
    assert len(kept_claims(_call(), many)) == 10
    assert len(promises(_call(), many)) == 3
    part = escalations_part(_call(), many, [])
    assert (len(part["claims"]), len(part["items"])) == (10, 3)


# --- in wave 2 ---------------------------------------------------------------------------


async def _wave2(escalations: list[Any]) -> tuple[FakeLLM, Any]:
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
    llm.script_for(ESCALATIONS_TEMPLATE, *escalations)
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
        stage1_escalations=[OFF_CHANNEL],
    )
    return llm, wave


async def test_wave2_merges_the_pass_into_the_escalations_part() -> None:
    llm, wave = await _wave2([json_response(ANSWER)])
    part = wave.parts[ESCALATIONS]
    assert part is not None and len(part["items"]) == 4
    (sent,) = [c for c in llm.calls if c.profile == PROFILE_UNIT_B_ESCALATIONS]
    assert sent.max_output_tokens == 2500


async def test_a_failed_escalations_pass_is_null_with_its_reason() -> None:
    _, wave = await _wave2([json_response({}), json_response({})])
    assert (wave.parts[ESCALATIONS], wave.reasons[ESCALATIONS]) == (
        None,
        "escalations_malformed_output",
    )
