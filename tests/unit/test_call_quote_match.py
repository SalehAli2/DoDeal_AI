"""The quote match (evidence.py): a quote's words in order in its segment, with
nothing between them but a repeated word or a filler, never a negation; and a
quote found next door stored at the segment it is in."""

from __future__ import annotations

import hashlib

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence import evidence
from dodeal_ai.units.call_intelligence.alarms import words
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    locate,
    quote_errors,
    relocated,
)
from dodeal_ai.units.call_intelligence.passes import Extraction, extract
from dodeal_ai.units.call_intelligence.prompts import AGENT, CLIENT
from dodeal_ai.units.call_intelligence.roles import Roles, check_roles, opening
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


def _one(text: str) -> CallText:
    transcript = Transcript.of((_say(0, "lead", text),), provider="f", model="f")
    return CallText.of(transcript, country_code="971")


def _check(quote: str, said: str) -> list[tuple[str, str]]:
    return quote_errors(_one(said), "here", quote, "s1")


# --- the guards --------------------------------------------------------------------


def test_a_repeated_word_in_the_segment_may_be_left_out_of_the_quote() -> None:
    assert _check("لا انا عمري زرت دبي", "لا انا عمري عمري زرت دبي") == []


def test_a_negation_left_out_of_the_quote_fails() -> None:
    """The guard: dropping ما turns "I have never been" into "I have been"."""
    said = "انا عمري ما زرت دبي"
    assert _check("انا عمري زرت دبي", said) == [("here", "quote_not_in_segment")]


def test_one_changed_word_fails() -> None:
    said = "انا عمري ما زرت دبي"
    assert _check("انا عمري ما زرت مصر", said) == [("here", "quote_not_in_segment")]
    assert _check("انا عمري ما زرت دبي", said) == []


def _eleven_twelve_thirteen() -> CallText:
    """Thirteen segments; s12 holds the words, s11 and s13 do not."""
    segments = [_say(n * 5, "lead", f"كلام عادي رقم {n}") for n in range(10)]
    segments += [
        _say(50, "agent", "طيب خلينا نتكلم عن الفيلا"),
        _say(55, "lead", "انا عمري ما زرت دبي بس ابغى اشتري"),
        _say(60, "agent", "ممتاز نرتب لك زيارة"),
    ]
    transcript = Transcript.of(tuple(segments), provider="f", model="f")
    return CallText.of(transcript, country_code="971")


def test_a_quote_found_in_s12_while_citing_s11_is_stored_as_s12() -> None:
    call = _eleven_twelve_thirteen()
    answer = Extraction.model_validate(
        {
            **_extraction(),
            "wanted": {
                "text": "Buy in Dubai.",
                "quote": "ما زرت دبي بس ابغى اشتري",
                "segment": "s11",
            },
            "agreed": [],
            "next_step": {
                "action": None,
                "owner": "unknown",
                "due": None,
                "quote": None,
                "segment": None,
            },
            "mood": {"value": "neutral", "quote": None, "segment": None},
        }
    )

    assert quote_errors(call, "wanted", "ما زرت دبي", "s11") == []
    assert locate(call, "ما زرت دبي", "s11") == "s12"
    assert locate(call, "ما زرت دبي", "s13") == "s12"
    moved = relocated(call, answer)
    assert moved.wanted is not None and moved.wanted.segment == "s12"
    assert moved.model_dump(exclude={"wanted"}) == answer.model_dump(exclude={"wanted"})


async def test_the_extract_pass_stores_the_segment_the_quote_is_in() -> None:
    call = _eleven_twelve_thirteen()
    answer = _extraction()
    answer.update(
        wanted={"text": "Buy.", "quote": "ابغى اشتري", "segment": "s11"},
        agreed=[{"text": "A visit.", "quote": "نرتب لك زيارة", "segment": "s12"}],
        next_step={
            "action": "A visit.",
            "owner": "agent",
            "due": None,
            "quote": "نرتب لك زيارة",
            "segment": "s13",
            "kind": "viewing",
        },
        mood={"value": "neutral", "quote": None, "segment": None},
    )
    llm = FakeLLM(json_response(answer))

    found, _ = await extract(llm, call, scope=SCOPE, settings=get_settings())

    assert found.wanted is not None
    assert (found.wanted.segment, found.agreed[0].segment) == ("s12", "s13")
    assert found.next_step.segment == "s13"
    assert llm.call_count == 1


# --- the rule's edges --------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("انا زرت دبي", "انا يعني زرت دبي"),
        ("انا زرت دبي", "انا اه ايوه زرت دبي"),
        ("I want the villa", "I um want uh the villa"),
        ("I want the villa", "I want want the the villa"),
        ("the price is fine", "Well, the price is, um, fine for me."),
    ],
)
def test_fillers_and_repeats_may_be_left_out(quote: str, said: str) -> None:
    assert _check(quote, said) == []


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("I did like it", "I did not like it"),
        ("I will buy", "I will never buy"),
        ("I want the villa", "I want only the villa"),
        ("انا زرت دبي", "انا مش زرت دبي"),
        ("انا زرت دبي", "انا اه ما زرت دبي"),
        ("انا زرت", "انا لا لا زرت"),
        ("زرت انا", "انا زرت"),
    ],
)
def test_anything_else_between_the_words_fails(quote: str, said: str) -> None:
    assert _check(quote, said) == [("here", "quote_not_in_segment")]


def test_a_repeated_negation_may_be_left_out_once_it_is_quoted() -> None:
    assert _check("لا انا ما زرت", "لا لا انا ما ما زرت") == []


def test_the_segment_before_is_looked_in_before_the_one_after() -> None:
    call = CallText.of(
        Transcript.of(
            (
                _say(0, "lead", "the villa is fine"),
                _say(5, "agent", "anything else"),
                _say(10, "lead", "the villa is fine"),
            ),
            provider="f",
            model="f",
        ),
        country_code="971",
    )
    assert locate(call, "the villa is fine", "s2") == "s1"
    assert locate(call, "the villa is fine", "s9") is None
    assert locate(call, "", "s2") is None
    assert locate(call, "nothing like it", "s2") is None


def test_the_speaker_is_read_at_the_segment_the_quote_is_in() -> None:
    call = _eleven_twelve_thirteen()
    assert quote_errors(call, "x", "ما زرت دبي", "s11", speaker=CLIENT) == []
    assert quote_errors(call, "x", "ما زرت دبي", "s11", speaker=AGENT) == [
        ("x", "quote_wrong_speaker")
    ]


def test_the_roles_check_reads_the_voice_where_the_quote_is() -> None:
    """A quote of speaker_1's words cited at speaker_2's segment is found in
    s1 and read as speaker_1's: a borrowed quote, not a missing one."""
    head = Transcript.of(
        (
            _say(0, "speaker_1", "Hello, who is calling please?"),
            _say(5, "speaker_2", "Hi, this is Nada from the company."),
            _say(10, "speaker_1", "Yes, I want a villa near the sea."),
        ),
        provider="f",
        model="f",
    )
    answer = Roles.model_validate(
        {
            "speakers": [
                {
                    "speaker": "speaker_1",
                    "role": "client",
                    "quote": "I want a villa",
                    "segment": "s3",
                },
                {
                    "speaker": "speaker_2",
                    "role": "agent",
                    "quote": "Hello, who is calling",
                    "segment": "s2",
                },
            ]
        }
    )
    with pytest.raises(OutputValidationError) as refused:
        check_roles(opening(head, country_code="971"))(answer)
    assert refused.value.errors == (("speakers.1", "quote_wrong_speaker"),)


# --- the list ----------------------------------------------------------------------


def test_no_filler_is_a_negation() -> None:
    fillers = set(words(" ".join(evidence.QUOTE_FILLERS)))
    assert fillers.isdisjoint(words(" ".join(evidence.NEGATIONS)))


def test_the_filler_list_is_pinned_to_its_version() -> None:
    """A change to the list without a new version fails here."""
    digest = hashlib.sha256("\n".join(evidence.QUOTE_FILLERS).encode()).hexdigest()
    assert (evidence.QUOTE_FILLERS_VERSION, digest) == (
        "quote_fillers_v1",
        "cb64d02b4da1cd83c8fc09388c506401847821c68f03d78cb35419c2669ef84f",
    )
