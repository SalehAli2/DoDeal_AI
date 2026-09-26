"""What every Unit B pass is checked against (waves 1 and 2): the transcript as
the model read it, the quote check, the language share, and the strict schema
types every pass's answer is built from.

THE TRANSCRIPT AS A PASS SEES IT (CallText): each segment's prompt copy --
numbers and emails masked under the tenant's country code (numbers.py) -- its
speaker's role, each side's language as the roles pass heard it, the summary
language decided in code from them (language.py), and whether the transcript
as a whole is uncertain.

THE QUOTE CHECK, in code, on every quote a pass returns: at most 40 words (the
prompts ask for 15); the segment it cites exists; and its words, normalised as
the alarm matcher normalises (alarms.py), appear in that segment in order.
Between two of them the segment may hold only words that repeat the word
before them ("عمري عمري") or are on QUOTE_FILLERS; a negation never is one.
Not in the cited segment, the quote is looked for in the one just before, then
the one just after, and where found there that segment is stored (relocated).
The segment is read as the model read it, so a quote can never carry a number
back out. Where a pass names who must have said it, the segment it is found in
is that speaker's. Any failure is a malformed answer: the pass is reprompted
once, then fails -- except where a pass says otherwise (passes.py).

THE LANGUAGE SHARE: a text is in the language asked for when at least 60 % of
its letters are in that language's script, counting none inside quotation
marks -- so a quoted phrase or one foreign name neither passes nor fails it.
A text with no letters outside quotes is in no language. A message may be in
any of MESSAGE_LANGUAGES, each held to its script (written_in): Arabic letters
for ar, ur and fa, Latin for en, fr and tr, Devanagari for hi, Cyrillic for ru
and Han for zh -- so French passes as Latin, never as not-English.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from dodeal_ai.units.call_intelligence.alarms import words
from dodeal_ai.units.call_intelligence.language import (
    UNHEARD,
    Spoken,
    SummaryLanguage,
    summary_language,
)
from dodeal_ai.units.call_intelligence.numbers import prompt_copy
from dodeal_ai.units.call_intelligence.prompts import (
    render_transcript,
    role_of,
    segment_id,
)
from dodeal_ai.units.call_intelligence.transcriber import (
    MIN_MEAN_CONFIDENCE,
    Segment,
    Transcript,
)

# The most words a quote may carry. 40 leaves headroom above the prompts' 15, so
# an exact quote a little long is kept, not reprompted. Lower sends true quotes
# back as quote_length; much higher lets a pasted paragraph pass as a quote.
MAX_QUOTE_WORDS = 40

# The share of a text's letters, outside quotes, that must be in the script of
# the language asked for.
MIN_SCRIPT_SHARE = 0.6

# The share of an answer's quotes that may fail and the answer still be kept
# field by field (the extraction, the extras): more than half failing is a
# model not reading the call. Lower reprompts answers with one slip; higher
# keeps answers mostly invented.
MAX_FAILED_QUOTE_SHARE = 0.5

# The hesitations a quote may leave out of its segment, Arabic and English:
# sounds that carry no meaning. Versioned: a change is a new version. A word
# that carries meaning here lets a quote drop it and still pass as exact.
QUOTE_FILLERS_VERSION = "quote_fillers_v1"
QUOTE_FILLERS: tuple[str, ...] = (
    "يعني",
    "اه",
    "آه",
    "ايوه",
    "إيوه",
    "امم",
    "اممم",
    "ممم",
    "um",
    "umm",
    "uh",
    "uhh",
    "uhm",
    "er",
    "erm",
    "hmm",
    "mm",
    "ah",
)
# The negations, Arabic and English, never skipped whatever QUOTE_FILLERS
# says: a quote that drops one says the opposite of the call.
NEGATIONS: tuple[str, ...] = (
    "ما",
    "مش",
    "مو",
    "مب",
    "لا",
    "لم",
    "لن",
    "ليس",
    "مافي",
    "ماني",
    "not",
    "no",
    "never",
    "nor",
    "neither",
    "none",
    "nothing",
    "nobody",
    "cannot",
    "don't",
    "doesn't",
    "didn't",
    "won't",
    "can't",
    "isn't",
    "aren't",
    "wasn't",
    "weren't",
)
_NEGATION_WORDS = frozenset(words(" ".join(NEGATIONS)))
_FILLER_WORDS = frozenset(words(" ".join(QUOTE_FILLERS))) - _NEGATION_WORDS

# Where a quote not in its cited segment is looked for next: the segment just
# before, then the one just after.
_NEIGHBOURS = (-1, 1)
# The fields a quote and its segment are held in, in every pass's answer.
_QUOTE_FIELDS = (
    ("quote", "segment"),
    ("said", "segment"),
    ("agent_quote", "agent_segment"),
    ("satisfied_quote", "satisfied_segment"),
    ("when_quote", "when_segment"),
)

# A span in double quotation marks, in the three forms a summary may use.
_QUOTED = re.compile(r'"[^"]*"|“[^”]*”|«[^»]*»')
_SCRIPTS: dict[SummaryLanguage, re.Pattern[str]] = {
    "ar": re.compile(
        r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]"
    ),
    "en": re.compile(r"[A-Za-z\u00c0-\u024f]"),
}

# The script each language a message may be written in is held to.
_MESSAGE_SCRIPTS: dict[str, re.Pattern[str]] = {
    "ar": _SCRIPTS["ar"],
    "ur": _SCRIPTS["ar"],
    "fa": _SCRIPTS["ar"],
    "en": _SCRIPTS["en"],
    "fr": _SCRIPTS["en"],
    "tr": _SCRIPTS["en"],
    "hi": re.compile(r"[\u0900-\u097f]"),
    "ru": re.compile(r"[\u0400-\u04ff]"),
    "zh": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]"),
}
MESSAGE_LANGUAGES: tuple[str, ...] = tuple(_MESSAGE_SCRIPTS)

type Errors = list[tuple[str, str]]

# Lengths past any honest answer: a quote or a sentence the size of the
# transcript is not one. A segment id is s1 to s99999.
_SENTENCE_CHARS = 400
_SEGMENT = r"^s[1-9][0-9]{0,4}$"

# A quote and its segment where either may be absent, then where both must be.
type Quote = Annotated[str | None, Field(max_length=_SENTENCE_CHARS)]
type SegmentId = Annotated[str | None, Field(pattern=_SEGMENT)]
type Said = Annotated[str, Field(min_length=1, max_length=_SENTENCE_CHARS)]
type Cited = Annotated[str, Field(pattern=_SEGMENT)]


class Strict(BaseModel):
    """An answer's shape, exactly: no field it does not name, never changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class CallText:
    """The transcript as every pass sees it: each segment's prompt copy and
    role, the summary language, whether the whole is uncertain, and each
    side's language as the roles pass heard it."""

    segments: tuple[Segment, ...]
    shown: tuple[str, ...]
    language: SummaryLanguage
    uncertain: bool
    spoken: Spoken = UNHEARD

    @classmethod
    def of(
        cls, transcript: Transcript, *, country_code: str, spoken: Spoken = UNHEARD
    ) -> CallText:
        """`country_code` is the tenant's, which a local number is masked under;
        `spoken` the roles pass's languages, unheard when it gave none."""
        return cls(
            segments=transcript.segments,
            shown=tuple(
                prompt_copy(segment.text, country_code=country_code)
                for segment in transcript.segments
            ),
            language=summary_language(transcript, spoken),
            uncertain=transcript.uncertain,
            spoken=spoken,
        )

    def data(self, stamps: Sequence[str] | None = None) -> str:
        """The transcript first, then the language line; `stamps`, one per
        segment, is when each was said, written on its line."""
        transcript = render_transcript(self.segments, self.shown, stamps)
        return f"TRANSCRIPT:\n{transcript}\n\nLANGUAGE: {self.language}"

    def index_of(self, segment: str) -> int | None:
        """The position of the segment an id names, or None for none."""
        index = int(segment[1:]) - 1
        return index if index < len(self.segments) else None

    def low_confidence(self, segment: str | None) -> bool:
        """Whether the segment an id names was rated below the floor; a
        segment with no rating is not."""
        index = None if segment is None else self.index_of(segment)
        confidence = None if index is None else self.segments[index].confidence
        return confidence is not None and confidence < MIN_MEAN_CONFIDENCE


def script_letters(text: str) -> dict[SummaryLanguage, int]:
    """How many of `text`'s letters are in each script, quotes included."""
    return {code: len(pattern.findall(text)) for code, pattern in _SCRIPTS.items()}


def script_language(text: str, hint: str | None) -> str:
    """A segment's language by the script most of its letters are in -- ar for
    Arabic, en for Latin, a tie ar -- for an engine that names none; with no
    letters, the hint when it is one of the two, else und."""
    letters = script_letters(text)
    if letters["ar"] or letters["en"]:
        return "ar" if letters["ar"] >= letters["en"] else "en"
    return hint if hint in ("ar", "en") else "und"


def _skippable(said: Sequence[str], at: int) -> bool:
    """Whether the word at `at` may sit between two of a quote's words: it
    repeats the word before it, or it is a filler (never a negation)."""
    return said[at] in _FILLER_WORDS or (at > 0 and said[at - 1] == said[at])


def _reachable(said: Sequence[str], start: int) -> Iterator[int]:
    """The positions the next quote word may stand at after one that ended at
    `start`: `start`, and each after a run of skippable words."""
    at = start
    while at < len(said):
        yield at
        if not _skippable(said, at):
            return
        at += 1


def _matches(said: Sequence[str], quote: Sequence[str]) -> bool:
    """Whether the quote's words stand in `said` in order, nothing between two
    of them but skippable words (module docstring)."""
    ends = {at + 1 for at, word in enumerate(said) if word == quote[0]}
    for wanted in quote[1:]:
        ends = {
            at + 1
            for start in ends
            for at in _reachable(said, start)
            if said[at] == wanted
        }
    return bool(ends)


def _found_at(call: CallText, quoted: Sequence[str], index: int) -> int | None:
    """The segment the quote's words are in: the cited one, else the one just
    before, else the one just after; None when none holds them."""
    for at in (index, *(index + step for step in _NEIGHBOURS)):
        if 0 <= at < len(call.shown) and _matches(words(call.shown[at]), quoted):
            return at
    return None


def found_at(call: CallText, quote: str, segment: str) -> int | None:
    """The position of the segment a quote is found in (_found_at), or None
    when the cited id is unknown, the quote's length is wrong or no segment
    holds it."""
    index = call.index_of(segment)
    quoted = words(quote)
    if index is None or not quoted or len(quoted) > MAX_QUOTE_WORDS:
        return None
    return _found_at(call, quoted, index)


def locate(call: CallText, quote: str, segment: str) -> str | None:
    """The id of the segment a quote is found in (found_at), or None."""
    found = found_at(call, quote, segment)
    return None if found is None else segment_id(found)


def relocated[M: BaseModel](call: CallText, answer: M) -> M:
    """The answer with every quote's segment the one the quote is found in:
    a quote found in a neighbour of the segment it cites is stored with the
    neighbour's id. Nothing else changes, and a failing quote stays as it is."""
    update: dict[str, object] = {}
    for name in type(answer).model_fields:
        value = getattr(answer, name)
        if isinstance(value, BaseModel):
            moved = relocated(call, value)
            if moved is not value:
                update[name] = moved
        elif isinstance(value, list):
            items = [
                relocated(call, item) if isinstance(item, BaseModel) else item
                for item in value
            ]
            if any(new is not old for new, old in zip(items, value, strict=True)):
                update[name] = items
    for quote_field, segment_field in _QUOTE_FIELDS:
        quote = getattr(answer, quote_field, None)
        segment = getattr(answer, segment_field, None)
        if isinstance(quote, str) and isinstance(segment, str):
            found = locate(call, quote, segment)
            if found is not None and found != segment:
                update[segment_field] = found
    return answer.model_copy(update=update) if update else answer


def quote_errors(
    call: CallText,
    where: str,
    quote: str | None,
    segment: str | None,
    *,
    speaker: str | None = None,
) -> Errors:
    """The quote check for one cited quote, which may be absent altogether;
    [] when it passes. `speaker`, when given, is who must have said it, in
    the segment the quote is found in."""
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
    found = _found_at(call, quoted, index)
    if found is None:
        return [(where, "quote_not_in_segment")]
    if speaker is not None and role_of(call.segments[found]) != speaker:
        return [(where, "quote_wrong_speaker")]
    return []


def evidence_errors(
    call: CallText,
    where: str,
    quote: str | None,
    segment: str | None,
    *,
    speaker: str | None = None,
) -> Errors:
    """The quote check for a quote that must be there."""
    if quote is None and segment is None:
        return [(where, "quote_missing")]
    return quote_errors(call, where, quote, segment, speaker=speaker)


def mostly_failed(quotes: dict[str, Errors]) -> bool:
    """Whether more than MAX_FAILED_QUOTE_SHARE of the quotes, each by where
    it is with its failures, failed."""
    failing = sum(1 for errors in quotes.values() if errors)
    return failing > MAX_FAILED_QUOTE_SHARE * len(quotes)


def failed_quotes(quotes: dict[str, Errors]) -> Errors:
    """Every failure of every quote, in order."""
    return [error for errors in quotes.values() for error in errors]


def in_language(text: str, language: SummaryLanguage) -> bool:
    """At least MIN_SCRIPT_SHARE of the letters outside quotes in the script."""
    return written_in(text, language)


def written_in(text: str, language: str) -> bool:
    """in_language for any of MESSAGE_LANGUAGES, by its script."""
    letters = [char for char in _QUOTED.sub(" ", text) if char.isalpha()]
    if not letters:
        return False
    script = _MESSAGE_SCRIPTS[language]
    asked = sum(1 for char in letters if script.match(char))
    return asked / len(letters) >= MIN_SCRIPT_SHARE
