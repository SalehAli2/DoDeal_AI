"""The alarm matcher (units/call_intelligence/alarms.py): the normalisation,
proclitics and up to two extra words, one find per phrase per segment, an
agent's match escalated and a client's only recorded, and the list's digest."""

from __future__ import annotations

import hashlib

import pytest

from dodeal_ai.units.call_intelligence.alarms import (
    alarm_list,
    alarm_list_digest,
    alarms_if_enabled,
    detect_alarms,
    normalise,
    phrase_in,
    words,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment

# Invented phrases, as a tenant might list them.
PHRASES = frozenset({"كلمني على الواتساب", "my personal number"})


def _say(speaker: str, text: str, start: float = 3.0) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 4,
        speaker=speaker,
        text=text,
        language="ar",
        confidence=0.9,
    )


# --- the guard -------------------------------------------------------------------


def test_a_variant_spelling_with_one_extra_word_matches_and_escalates() -> None:
    """ى for ي, a diacritic, a tatweel, ب+ال on WhatsApp and one word between."""
    said = _say("agent", "خلاص كلمنـى بس علَى بالواتساب بكرة", start=41.5)
    found = detect_alarms((_say("agent", "أهلاً", start=0), said), PHRASES)
    index = alarm_list(PHRASES).index("كلمني علي الواتساب")
    where = {"speaker": "agent", "start_s": 41.5, "segment": "s2"}
    assert found.finds == [{"phrase": index, **where}]
    assert found.escalations == [
        {
            "type": "off_channel_contact",
            "source": "alarm_phrase",
            "phrase": index,
            **where,
        }
    ]


def test_a_client_saying_it_is_recorded_and_does_not_escalate() -> None:
    found = detect_alarms((_say("lead", "كلمني على الواتساب"),), PHRASES)
    assert [f["speaker"] for f in found.finds] == ["client"]
    assert found.escalations == []


# --- the normalisation -----------------------------------------------------------


def test_the_normalisation_folds_letters_and_drops_marks() -> None:
    assert normalise("أإآٱ ى ی ة ـ َ ّ RÉSUMÉ") == "اااا ي ي ه    résumé"
    assert words("Call ME, now!") == ["call", "me", "now"]


# --- the matching ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "matches"),
    [
        ("this is my personal number", True),
        ("this is my own personal number", True),
        ("this is my own personal mobile phone number", True),
        ("my very own and personal number", False),
        ("personal number my", False),
        ("my number", False),
    ],
)
def test_up_to_two_extra_words_between_phrase_words(said: str, matches: bool) -> None:
    assert phrase_in(words("my personal number"), words(said)) is matches


@pytest.mark.parametrize(
    ("said", "matches"),
    [
        ("واتساب", True),
        ("والواتساب", True),
        ("بالواتساب", True),
        ("للواتساب", True),
        ("فبالواتساب", True),
        ("كواتساب", False),
        ("سواتساب", False),
    ],
)
def test_arabic_proclitics_may_lead_a_phrase_word(said: str, matches: bool) -> None:
    assert phrase_in(["واتساب"], words(said)) is matches


def test_a_later_occurrence_is_tried_when_the_first_leads_nowhere() -> None:
    assert phrase_in(["a", "b", "c"], ["a", "b", "x", "b", "x", "x", "c"])


def test_an_empty_phrase_matches_nothing() -> None:
    assert not phrase_in([], ["anything"])


def test_one_find_per_phrase_per_segment() -> None:
    said = _say("lead", "my personal number, yes my personal number")
    assert len(detect_alarms((said,), PHRASES).finds) == 1


# --- the list and its digest -----------------------------------------------------


def test_the_digest_is_over_the_sorted_normalised_list() -> None:
    listed = ["b phrase", "إلى الواتساب", "a phrase"]
    expected = hashlib.sha256(
        "\n".join(sorted(["a phrase", "b phrase", "الي الواتساب"])).encode()
    ).hexdigest()
    assert alarm_list_digest(listed) == expected
    assert alarm_list_digest(reversed(listed)) == expected
    assert detect_alarms((), listed).digest == expected


def test_the_switch_off_matches_nothing() -> None:
    segments = (_say("agent", "my personal number"),)
    assert alarms_if_enabled(segments, enabled=False, phrases=PHRASES) is None
    found = alarms_if_enabled(segments, enabled=True, phrases=PHRASES)
    assert found is not None and len(found.escalations) == 1
