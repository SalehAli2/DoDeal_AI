"""The quote match (evidence.py): a quote's words in order in its segment, with
nothing between them but a repeated word or a filler, never a negation; and a
quote found next door stored at the segment it is in."""

from __future__ import annotations

import hashlib

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.units.call_intelligence import evidence
from dodeal_ai.units.call_intelligence.alarms import words
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    locate,
    own_words,
    quote_errors,
    relocated,
)
from dodeal_ai.units.call_intelligence.passes import Extraction, extract, settled
from dodeal_ai.units.call_intelligence.prompts import AGENT, CLIENT
from dodeal_ai.units.call_intelligence.roles import Roles, judge, opening
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
        ("انا زرت دبي", "انا مش زرت دبي"),
        ("انا زرت دبي", "انا اه ما زرت دبي"),
        ("انا زرت", "انا لا لا زرت"),
        ("زرت انا", "انا زرت"),
    ],
)
def test_anything_else_between_the_words_fails(quote: str, said: str) -> None:
    assert _check(quote, said) == [("here", "quote_not_in_segment")]


# --- D-103: the quote locates, the transcript speaks -------------------------------

# An invented disfluent budget question, and a quote of it that leaves the
# hesitation "مثلا" out -- the shape of the miss that cost a real call its
# score: every quoted word there, in order, one left out.
BUDGET_SAID = "طب حضرتك ناوي مثلا تحط كام في الاستثمار ده السنة دي"
BUDGET_QUOTED = "حضرتك ناوي تحط كام في الاستثمار ده"


def test_a_word_left_out_of_disfluent_speech_passes_as_the_transcripts_words() -> None:
    """The guard: the quote locates, and what is stored is the transcript's
    own words from its first word to its last, the left-out one included."""
    call = _one(BUDGET_SAID)
    assert quote_errors(call, "asked_budget", BUDGET_QUOTED, "s1") == []
    assert own_words(call, BUDGET_QUOTED, 0) == (
        "حضرتك ناوي مثلا تحط كام في الاستثمار ده"
    )


@pytest.mark.parametrize(
    ("quote", "said", "passes"),
    [
        ("I want the villa", "I want only the villa", True),
        ("want villa", "want the villa", False),
        ("I want villa", "I want the villa", True),
        ("I want villa", "I want the big villa", False),
        ("I really want that villa now", "I really do want that big villa now", True),
        (
            "I really want that villa now",
            "I really do want that big sea villa now",
            False,
        ),
        ("we can meet tomorrow", "we can maybe if you like meet tomorrow", False),
    ],
    ids=[
        "one-of-four",
        "two-words-none",
        "one-of-three",
        "two-of-three",
        "two-of-six",
        "three-of-six",
        "a-run-of-four",
    ],
)
def test_how_many_words_a_quote_may_leave_out(
    quote: str, said: str, passes: bool
) -> None:
    """One in OMIT_EVERY (3) quote words, none under OMIT_MIN_WORDS (3), and
    at most OMIT_RUN (3) in one gap; fillers and repeats free."""
    assert (evidence.OMIT_MIN_WORDS, evidence.OMIT_EVERY, evidence.OMIT_RUN) == (
        3,
        3,
        3,
    )
    assert (_check(quote, said) == []) is passes


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("انا السعر عالي عليا", "انا ماعرفتش السعر عالي عليا"),
        ("هيجيلك العقد كامل النهاردة", "هيجيلك العقد كامل مفيش النهاردة"),
        ("ندفع المبلغ كله كاش", "ندفع المبلغ بدون كله كاش"),
        ("كل الوحدات متاحة للبيع", "كل الوحدات الا متاحة للبيع"),
        ("the unit comes furnished today", "the unit comes without furnished today"),
    ],
    ids=["fused-negation", "mafish", "bidoun", "illa", "without"],
)
def test_a_word_that_turns_the_sentence_is_never_left_out(
    quote: str, said: str
) -> None:
    assert _check(quote, said) == [("here", "quote_not_in_segment")]


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("I want the big villa", "I want the villa"),
        ("I want the small villa", "I want the big villa"),
        ("the villa I want", "I want the villa"),
    ],
    ids=["a-word-added", "a-word-changed", "reordered"],
)
def test_a_word_added_changed_or_moved_still_fails(quote: str, said: str) -> None:
    assert _check(quote, said) == [("here", "quote_not_in_segment")]


def test_the_place_leaving_out_fewest_words_is_stored() -> None:
    """Said twice, once with a word between: the exact place is stored."""
    call = _one("I want the big villa, yes, I want the villa")
    assert own_words(call, "I want the villa", 0) == "I want the villa"


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
    s1, speaker_1's: it never verifies speaker_2 (A1), who takes the role
    the verified client leaves."""
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
    judged = judge(opening(head, country_code="971"), answer)
    assert judged.verified == (True, False) and judged.errors == ()
    assert judged.speakers[1].quote is None


# --- the list ----------------------------------------------------------------------


def test_no_filler_is_a_negation() -> None:
    fillers = set(words(" ".join(evidence.QUOTE_FILLERS)))
    assert fillers.isdisjoint(words(" ".join(evidence.NEGATIONS)))


def test_the_filler_list_is_pinned_to_its_version() -> None:
    """A change to the list without a new version fails here."""
    digest = hashlib.sha256("\n".join(evidence.QUOTE_FILLERS).encode()).hexdigest()
    assert (evidence.QUOTE_FILLERS_VERSION, digest) == (
        "quote_fillers_v2",
        "eeb894bcb5ad3588f5c4d2a927d7bfb0824fea79fab4ba04ca835f9f7897b977",
    )


# --- A4: folds, proclitics, negations, the stored words ----------------------------

MISSED = [("here", "quote_not_in_segment")]


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("المسؤول عن المشروع", "المسوول عن المشروع"),
        ("جئت امس", "جيت امس"),
        ("إنت فين", "انت فين"),
        ("عندي ٣ غرف", "عندي 3 غرف"),
        ("السعر 1,500,000 درهم", "السعر ١٬٥٠٠٬٠٠٠ درهم"),
        ("رقم ۷", "رقم 7"),
    ],
)
def test_hamza_seats_and_arabic_indic_digits_are_folded(quote: str, said: str) -> None:
    """The guard (A4): a seat or a digit written the other way still matches."""
    assert _check(quote, said) == []
    assert _check(said, quote) == []


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("البيت حلو", "والبيت حلو"),
        ("بالتقسيط", "التقسيط"),
        ("فالسعر عالي", "السعر عالي"),
        ("للبيع", "لبيع"),
        ("والسعر عالي", "و السعر عالي"),
        ("و السعر عالي", "والسعر عالي"),
        ("السعر عالي", "و السعر عالي"),
    ],
)
def test_one_proclitic_and_a_detached_and_are_allowed(quote: str, said: str) -> None:
    assert _check(quote, said) == []


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("لما وصلت", "ما وصلت"),
        ("ما وصلت", "بما وصلت"),
        ("لا اريد", "ولا اريد"),
        ("فلا اريد", "لا اريد"),
        ("يس عندي", "ليس عندي"),
        ("ما اريد", "و ما اريد"),
        ("و", "و ب"),
    ],
)
def test_a_proclitic_never_turns_a_negation_into_another_word(
    quote: str, said: str
) -> None:
    """The guard (A4): a negation is matched only by itself, never with a
    proclitic taken off or put on, and a one-letter rest is no word."""
    assert _check(quote, said) == MISSED


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("انا عايز الشقة", "انا مش عايز الشقة"),
        ("انا اريد", "انا لا اريد"),
        ("i want it", "i do not want it"),
    ],
)
def test_a_negation_is_never_skipped(quote: str, said: str) -> None:
    assert _check(quote, said) == MISSED


@pytest.mark.parametrize(
    ("quote", "said"),
    [
        ("i can do it", "i can't do it"),
        ("we won sell it", "we won't sell it"),
        ("i do know", "i don't know"),
    ],
)
def test_a_contraction_negates_by_its_t_and_is_never_lost(
    quote: str, said: str
) -> None:
    """The guard: "can't" is read as "can" "t"; the "t" is the negation, so
    a quote without it fails."""
    assert _check(quote, said) == MISSED
    assert _check(said, said) == []


def test_a_contractions_stem_alone_is_an_ordinary_word() -> None:
    """ "can", "don" and "won" are words, not negations: a sentence that only
    has them turns nothing around (D-104 reads certainty past them)."""
    assert [evidence.turns_around(w) for w in ("can", "don", "won", "t")] == [
        False,
        False,
        False,
        True,
    ]


def test_the_egyptian_fillers_of_list_v2_are_skipped() -> None:
    assert _check("الشقة حلوة خالص", "الشقة ده حلوة بقى خالص") == []


def test_the_transcripts_own_words_are_stored_as_the_quote() -> None:
    """A4: a quote found is stored as the segment's own words, first matched
    to last, as the prompt showed them, fillers between included."""
    call = CallText.of(
        Transcript.of(
            (
                _say(0, "agent", "Hello, the villa is ready."),
                _say(5, "lead", "و السعر يعني عالي جدا، honestly."),
            ),
            provider="f",
            model="f",
        ),
        country_code="971",
    )
    answer = Extraction.model_validate(
        {
            **_extraction(),
            "concerns": [
                {"text": "price", "quote": "والسعر عالي", "segment": "s1"},
                {"text": "villa", "quote": "the Villa is ready.", "segment": "s1"},
            ],
        }
    )
    kept = relocated(call, answer).concerns
    assert [(c.quote, c.segment) for c in kept] == [
        ("و السعر يعني عالي", "s2"),
        ("the villa is ready", "s1"),
    ]


def test_a_missing_quote_on_an_item_or_wanted_is_unverified_not_refused() -> None:
    raw = _extraction()
    raw["wanted"] = {"text": "A villa.", "quote": None, "segment": None}
    raw["concerns"] = [{"text": "price", "quote": None, "segment": None}]
    answer = Extraction.model_validate(raw)
    kept = settled(answer, _one("hello"))
    assert kept["wanted"]["unverified"] is True
    assert kept["concerns"] == [
        {"text": "price", "quote": None, "segment": None, "unverified": True}
    ]


def test_no_words_are_stored_for_a_quote_the_segment_does_not_hold() -> None:
    call = _one("السعر عالي جدا")
    assert evidence.own_words(call, "السعر عالي", 0) == "السعر عالي"
    assert evidence.own_words(call, "السعر رخيص", 0) is None
    assert evidence.own_words(call, " ".join(["السعر"] * 41), 0) is None
