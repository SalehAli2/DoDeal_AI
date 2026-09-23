"""Alarm phrases said on a call (Unit B): the tenant's list, matched in code
against every segment, and an agent's match escalated.

THE NORMALISATION, applied to the list and to the transcript alike, and the
one the quote check reuses (passes.py): casefolded; alef forms (أ إ آ ٱ) as
ا, alef maqsura and Farsi ya (ى ی) as ي, ta marbuta (ة) as ه; tatweel and
diacritics removed. Words are then the runs of letters and digits.

A PHRASE MATCHES a segment when its words appear in order, each word of the
segment allowed Arabic proclitics in front of the phrase's word -- و or ف,
then ب or ل, then ال (and the contracted لل) -- and at most two other words
between one phrase word and the next. One find per phrase per segment.

A FIND carries the phrase's index in the sorted list, who said it, when and
which segment; never the words. An agent's match is an off_channel_contact
escalation: the list is the tenant's words for taking a client off the
company's channels, and a client saying them is only recorded.

alarm_list_digest is SHA-256 over the sorted, normalised list, one phrase per
line: stage 1 carries it so a match can be read against the list in force.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from dodeal_ai.units.call_intelligence.prompts import AGENT, role_of, segment_id
from dodeal_ai.units.call_intelligence.transcriber import Segment

OFF_CHANNEL = "off_channel_contact"

# Words another word may sit between two of a phrase's words.
MAX_GAP_WORDS = 2

_FOLDS = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ی": "ي",
        "ة": "ه",
    }
)
# Diacritics, the superscript alef and the tatweel.
_MARKS = re.compile(r"[\u064b-\u065f\u0670\u0640]")
_WORD = re.compile(r"\w+")

# Every proclitic run a phrase word may carry: conjunction, then preposition,
# then the article; ل before ال contracts to لل.
_PROCLITICS: frozenset[str] = frozenset(
    conjunction + rest
    for conjunction in ("", "و", "ف")
    for rest in (
        *(
            preposition + article
            for preposition in ("", "ب", "ل")
            for article in ("", "ال")
        ),
        "لل",
    )
)


def normalise(text: str) -> str:
    """The text folded as the module docstring says."""
    return _MARKS.sub("", text.casefold()).translate(_FOLDS)


def words(text: str) -> list[str]:
    """The normalised words of `text`."""
    return _WORD.findall(normalise(text))


def _same_word(said: str, wanted: str) -> bool:
    if not said.endswith(wanted):
        return False
    return said[: len(said) - len(wanted)] in _PROCLITICS


def phrase_in(phrase: Sequence[str], said: Sequence[str]) -> bool:
    """Whether the phrase's words appear in `said` in order, each with its
    proclitics, at most MAX_GAP_WORDS apart."""

    def after(position: int, index: int) -> bool:
        if index == len(phrase):
            return True
        stop = min(len(said), position + MAX_GAP_WORDS + 2)
        return any(
            _same_word(said[at], phrase[index]) and after(at, index + 1)
            for at in range(position + 1, stop)
        )

    return bool(phrase) and any(
        _same_word(word, phrase[0]) and after(at, 1) for at, word in enumerate(said)
    )


def alarm_list(phrases: Iterable[str]) -> list[str]:
    """The tenant's phrases normalised and sorted: the order an index names."""
    return sorted({normalise(phrase).strip() for phrase in phrases} - {""})


def alarm_list_digest(phrases: Iterable[str]) -> str:
    """SHA-256 hex of the normalised, sorted list, one phrase per line."""
    return hashlib.sha256("\n".join(alarm_list(phrases)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AlarmFindings:
    """What the matcher found, the escalations it raises, and the list's digest."""

    finds: list[dict[str, object]]
    escalations: list[dict[str, object]]
    digest: str


def detect_alarms(segments: Sequence[Segment], phrases: Iterable[str]) -> AlarmFindings:
    """Every phrase of the list in every segment."""
    listed = alarm_list(phrases)
    wanted = [words(phrase) for phrase in listed]
    finds: list[dict[str, object]] = []
    escalations: list[dict[str, object]] = []
    for index, segment in enumerate(segments):
        said = words(segment.text)
        role = role_of(segment)
        for number, phrase in enumerate(wanted):
            if not phrase_in(phrase, said):
                continue
            where = {
                "speaker": role,
                "start_s": segment.start_s,
                "segment": segment_id(index),
            }
            finds.append({"phrase": number, **where})
            if role == AGENT:
                escalations.append(
                    {
                        "type": OFF_CHANNEL,
                        "source": "alarm_phrase",
                        "phrase": number,
                        **where,
                    }
                )
    return AlarmFindings(finds, escalations, alarm_list_digest(listed))


def alarms_if_enabled(
    segments: Sequence[Segment], *, enabled: bool, phrases: Iterable[str]
) -> AlarmFindings | None:
    """detect_alarms under the tenant's alarm_phrases_enabled switch; None,
    and nothing matched, while it is off."""
    return detect_alarms(segments, phrases) if enabled else None
