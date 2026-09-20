"""Register item 132: a short note that is a known outcome passes the length gate."""

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
