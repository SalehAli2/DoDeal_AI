"""What every Unit B pass is checked against (waves 1 and 2): the transcript as
the model read it, the quote check, and the language share.

THE TRANSCRIPT AS A PASS SEES IT (CallText): each segment's prompt copy --
numbers and emails masked under the tenant's country code (numbers.py) -- its
speaker's role, the summary language decided in code (language.py), and
whether the transcript as a whole is uncertain.

THE QUOTE CHECK, in code, on every quote a pass returns: at most 25 words; the
segment it cites exists; and its words, normalised as the alarm matcher
normalises (alarms.py), appear in that segment in order and unbroken. The
segment is read as the model read it, so a quote can never carry a number back
out. Any failure is a malformed answer: the pass is reprompted once, then fails.

THE LANGUAGE SHARE: a text is in the language asked for when at least 60 % of
its letters are in that language's script, counting none inside quotation
marks -- so a quoted phrase or one foreign name neither passes nor fails it.
A text with no letters outside quotes is in no language.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from dodeal_ai.units.call_intelligence.alarms import words
from dodeal_ai.units.call_intelligence.language import (
    SummaryLanguage,
    summary_language,
)
from dodeal_ai.units.call_intelligence.numbers import prompt_copy
from dodeal_ai.units.call_intelligence.prompts import render_transcript
from dodeal_ai.units.call_intelligence.transcriber import (
    MIN_MEAN_CONFIDENCE,
    Segment,
    Transcript,
)

MAX_QUOTE_WORDS = 25

# The share of a text's letters, outside quotes, that must be in the script of
# the language asked for.
MIN_SCRIPT_SHARE = 0.6

# A span in double quotation marks, in the three forms a summary may use.
_QUOTED = re.compile(r'"[^"]*"|“[^”]*”|«[^»]*»')
_SCRIPTS: dict[SummaryLanguage, re.Pattern[str]] = {
    "ar": re.compile(
        r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]"
    ),
    "en": re.compile(r"[A-Za-z\u00c0-\u024f]"),
}

type Errors = list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class CallText:
    """The transcript as every pass sees it: each segment's prompt copy and
    role, the summary language, and whether the whole is uncertain."""

    segments: tuple[Segment, ...]
    shown: tuple[str, ...]
    language: SummaryLanguage
    uncertain: bool

    @classmethod
    def of(cls, transcript: Transcript, *, country_code: str) -> CallText:
        """`country_code` is the tenant's, which a local number is masked under."""
        return cls(
            segments=transcript.segments,
            shown=tuple(
                prompt_copy(segment.text, country_code=country_code)
                for segment in transcript.segments
            ),
            language=summary_language(transcript),
            uncertain=transcript.uncertain,
        )

    def data(self) -> str:
        """The transcript first, then the language line."""
        transcript = render_transcript(self.segments, self.shown)
        return f"TRANSCRIPT:\n{transcript}\n\nLANGUAGE: {self.language}"

    def index_of(self, segment: str) -> int | None:
        """The position of the segment an id names, or None for none."""
        index = int(segment[1:]) - 1
        return index if index < len(self.segments) else None

    def low_confidence(self, segment: str | None) -> bool:
        index = None if segment is None else self.index_of(segment)
        return index is not None and (
            self.segments[index].confidence < MIN_MEAN_CONFIDENCE
        )


def _contains(said: Sequence[str], quote: Sequence[str]) -> bool:
    size = len(quote)
    return any(said[at : at + size] == quote for at in range(len(said) - size + 1))


def quote_errors(
    call: CallText,
    where: str,
    quote: str | None,
    segment: str | None,
) -> Errors:
    """The quote check for one cited quote, which may be absent altogether;
    [] when it passes."""
    if quote is None and segment is None:
        return []
    if quote is None or segment is None:
        return [(where, "quote_without_segment")]
    index = call.index_of(segment)
    if index is None:
        return [(where, "segment_unknown")]
    quoted = words(quote)
    if not quoted or len(quoted) > MAX_QUOTE_WORDS:
        return [(where, "quote_length")]
    if not _contains(words(call.shown[index]), quoted):
        return [(where, "quote_not_in_segment")]
    return []


def evidence_errors(
    call: CallText,
    where: str,
    quote: str | None,
    segment: str | None,
) -> Errors:
    """The quote check for a quote that must be there."""
    if quote is None and segment is None:
        return [(where, "quote_missing")]
    return quote_errors(call, where, quote, segment)


def in_language(text: str, language: SummaryLanguage) -> bool:
    """At least MIN_SCRIPT_SHARE of the letters outside quotes in the script."""
    letters = [char for char in _QUOTED.sub(" ", text) if char.isalpha()]
    if not letters:
        return False
    script = _SCRIPTS[language]
    asked = sum(1 for char in letters if script.match(char))
    return asked / len(letters) >= MIN_SCRIPT_SHARE
