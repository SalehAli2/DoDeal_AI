"""Register item 132: a short note that is a known outcome passes the length gate.

Register item 137 tightened four things, and every one of them is tested in
both scripts, because the corpus writes the same outcome in either.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from dodeal_ai.schemas.lead import LeadNote
from dodeal_ai.units.structured_intelligence import pipeline
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import (
    _is_recognised_short_note,
    _length_gate,
    _recognised_short,
)

CONFIG = get_tenant_config("tenant-a")


def _note(text: str) -> LeadNote:
    return LeadNote(
        id=1, note=text, author=None, author_id=1, createdAt=datetime.now(UTC)
    )


@pytest.mark.parametrize("text", ["na1", "cb1 tmrw", "not interested", "مش مهتم"])
def test_known_short_notes_are_recognised_and_pass_the_gate(text: str) -> None:
    """A code or phrase note is recognised and the gate lets it through."""
    assert _is_recognised_short_note(text, CONFIG)
    assert _length_gate(_note(text), CONFIG) is None


@pytest.mark.parametrize("text", ["ok", "done", "spoke", "", "   "])
def test_other_short_notes_are_not_recognised(text: str) -> None:
    """An unknown short note keeps today's suppression."""
    assert not _is_recognised_short_note(text.strip(), CONFIG)
    assert _length_gate(_note(text), CONFIG) is not None


def test_a_note_at_or_above_the_floor_never_reaches_recognition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recognition check is only consulted on the too-short branch."""

    def _boom(text: str, config: object) -> bool:
        raise AssertionError("recognition reached")

    monkeypatch.setattr(pipeline, "_is_recognised_short_note", _boom)
    assert _length_gate(_note("called the client, viewing on friday"), CONFIG) is None


def test_a_tenant_with_empty_tables_recognises_nothing() -> None:
    """Empty code and phrase tables recognise nothing, so every short note stops."""
    empty = dataclasses.replace(
        CONFIG, short_note_codes=frozenset(), short_note_phrases=frozenset()
    )
    for text in ("na1", "cb1 tmrw", "not interested", "مش مهتم"):
        assert not _is_recognised_short_note(text, empty)
        assert _length_gate(_note(text), empty) is not None


def test_every_word_must_be_a_code_or_a_filler() -> None:
    """A code followed by ordinary words is a real note, not an outcome."""
    assert _is_recognised_short_note("cb1 tmrw", CONFIG)
    assert _is_recognised_short_note("na1", CONFIG)
    assert not _is_recognised_short_note("na, client abusive, wants refund", CONFIG)


def test_the_arabic_phrase_and_filler_are_recognised() -> None:
    """The default Arabic phrase and filler match."""
    assert _is_recognised_short_note("لا يرد", CONFIG)
    assert _is_recognised_short_note("cb بكرة", CONFIG)


def test_a_tenant_with_no_fillers_still_recognises_a_bare_code() -> None:
    """An empty filler table drops the fillers only."""
    bare = dataclasses.replace(CONFIG, short_note_fillers=frozenset())
    assert _is_recognised_short_note("na1", bare)
    assert not _is_recognised_short_note("cb1 tmrw", bare)


def test_mixed_case_tables_match_however_the_config_was_built() -> None:
    """A directly built config with mixed-case tables is folded like a JSON one."""
    mixed = dataclasses.replace(
        CONFIG,
        short_note_codes=frozenset({"NA", "Cb"}),
        short_note_phrases=frozenset({"Not Interested"}),
        short_note_fillers=frozenset({"TMRW"}),
    )
    assert _is_recognised_short_note("cb1 tmrw", mixed)
    assert _is_recognised_short_note("NA1", mixed)
    assert _is_recognised_short_note("not interested", mixed)
    assert mixed.short_note_codes == frozenset({"na", "cb"})


# --- register item 137 ------------------------------------------------------


@pytest.mark.parametrize("text", ["tmrw", "again", "pm", "tmrw am", "بكرة", "النهاردة"])
def test_fillers_alone_are_not_an_outcome(text: str) -> None:
    """Every word a filler and no code records nothing, so it is not recognised."""
    assert not _is_recognised_short_note(text, CONFIG)
    assert _length_gate(_note(text), CONFIG) is not None


@pytest.mark.parametrize("text", ["na.", "cb1,", "na!", "لا يرد.", "مش مهتم،"])
def test_trailing_punctuation_does_not_hide_a_code_or_a_phrase(text: str) -> None:
    """A note picks up a full stop or a comma; the outcome is the same."""
    assert _is_recognised_short_note(text, CONFIG)
    assert _length_gate(_note(text), CONFIG) is None


@pytest.mark.parametrize("text", ["na 2", "cb 3 tmrw", "cb ٢", "na ٣ بكرة"])
def test_a_bare_digit_is_an_attempt_number(text: str) -> None:
    """The attempt number is written apart from the code as often as joined."""
    assert _is_recognised_short_note(text, CONFIG)


def test_a_bare_digit_alone_is_not_an_outcome() -> None:
    """A number with no code beside it records nothing."""
    assert not _is_recognised_short_note("2", CONFIG)
    assert not _is_recognised_short_note("٢", CONFIG)


@pytest.mark.parametrize(
    "text", ["no answer tmrw", "not interested", "لا يرد بكرة", "مش مهتم النهاردة"]
)
def test_a_phrase_may_open_the_note_and_be_followed_by_fillers(text: str) -> None:
    """A phrase plus a filler is the same outcome as the phrase alone."""
    assert _is_recognised_short_note(text, CONFIG)


@pytest.mark.parametrize(
    "text", ["no answer client angry", "لا يرد وعايز يلغي", "no answer cb1"]
)
def test_a_phrase_followed_by_ordinary_words_is_a_real_note(text: str) -> None:
    """Only fillers may follow the phrase; anything else is content to judge."""
    assert not _is_recognised_short_note(text, CONFIG)


# --- what the outcome line and the diagnostic column report -----------------


def test_a_note_above_the_floor_is_never_a_recognised_short_note() -> None:
    """`_recognised_short` is about the too-short BRANCH, not the phrase table.

    "not interested tmrw again" clears both floors, so the branch never ran --
    but the phrase table recognises it, and the flag read off the table alone
    counted a full note as one the floor would have refused.
    """
    full = _note("not interested tmrw again")
    assert _is_recognised_short_note(full.note, CONFIG)
    assert _length_gate(full, CONFIG) is None
    assert not _recognised_short(full, CONFIG)


def test_the_flag_is_true_only_where_the_branch_let_a_note_through() -> None:
    """Below the floor and recognised: true. Below it and not: false."""
    assert _recognised_short(_note("na1"), CONFIG)
    assert not _recognised_short(_note("too thin"), CONFIG)
