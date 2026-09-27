"""What every Unit B pass is checked against (waves 1 and 2): the transcript as
the model read it, the quote check, the language share, and the strict schema
types every pass's answer is built from.

THE TRANSCRIPT AS A PASS SEES IT (CallText): each segment's prompt copy --
numbers and emails masked under the tenant's country code (numbers.py) -- its
speaker's role, each side's language as the roles pass heard it, the summary
language decided in code from them (language.py), whether the transcript as a
whole is uncertain, and whether its words are: a transcript doubted only
because its roles were not applied (ROLES_NOT_APPLIED) has its words as heard.

THE QUOTE CHECK, in code, on every quote a pass returns: at most 40 words (the
prompts ask for 15); the segment it cites exists; and its words appear in that
segment in order (A4). Words are normalised as the alarm matcher normalises
(alarms.py), then every hamza seat (أ إ آ ؤ ئ) is folded to its base letter
and every Arabic-Indic digit to ASCII (quote_words); a detached و is joined to
the word after it. Two words are the same when equal, or when neither is a
negation and one is the other with one leading و, ف, ب or ل (the rest at
least two letters). Between two quote words the segment may hold only words
that repeat the word before them ("عمري عمري") or are on QUOTE_FILLERS; a
negation is never skipped, never added and never matched to another word.
Not in the cited segment, the quote is looked for in the one just before, then
the one just after, then -- a quote of FAR_MIN_WORDS words or more -- in every
other segment, nearest first (D-102: the words were said, only the id was
wrong); where found, the segment it is in and the transcript's own words,
first matched to last, are stored as the quote (relocated). A quote refused
as quote_not_in_segment is logged as quote_miss with why (miss_kind): codes
and counts, never its words.
The segment is read as the model read it, so a quote can never carry a number
back out. Where a pass names who must have said it, the segment it is found in
is that speaker's: a voice no role mapping named (prompts.said_by) is no one's,
so such a check fails there (quote_speaker_unknown). Any failure is a
malformed answer: the pass is reprompted once, then fails -- except where a
pass says otherwise (passes.py, extras.py; score.py and coaching.py keep the
verified parts of both answers, D-101). A quote failing only on an unknown voice is
never the model's slip, so it never counts toward mostly_failed.

THE LANGUAGE SHARE: a text is in the language asked for when at least 60 % of
its letters are in that language's script, counting none inside quotation
marks -- so a quoted phrase or one foreign name neither passes nor fails it.
A text with no letters outside quotes is in no language. A message may be in
any of MESSAGE_LANGUAGES, each held to its script (written_in): Arabic letters
for ar, ur and fa, Latin for en, fr and tr, Devanagari for hi, Cyrillic for ru
and Han for zh -- so French passes as Latin, never as not-English.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
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
    UNKNOWN,
    one_line,
    render_transcript,
    said_by,
    segment_id,
)
from dodeal_ai.units.call_intelligence.transcriber import (
    MIN_MEAN_CONFIDENCE,
    Segment,
    Transcript,
    is_uncertain,
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
QUOTE_FILLERS_VERSION = "quote_fillers_v2"
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
    # quote_fillers_v2: the Egyptian hesitations "ده" and "بقى".
    "ده",
    "بقى",
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
# The hamza seats and the Arabic-Indic digits (both forms), folded after the
# alarm matcher's normalisation for quotes only: the model writes a seat or a
# digit one way where the engine wrote the other.
_QUOTE_FOLDS = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ؤ": "و",
        "ئ": "ي",
        **{chr(0x0660 + digit): str(digit) for digit in range(10)},
        **{chr(0x06F0 + digit): str(digit) for digit in range(10)},
    }
)
# The Arabic-Indic digits alone as ASCII, for reading a number (numbers_in).
_DIGITS = str.maketrans(
    {
        **{chr(0x0660 + digit): str(digit) for digit in range(10)},
        **{chr(0x06F0 + digit): str(digit) for digit in range(10)},
    }
)
# A number as written: digits, with , . or the Arabic separators between
# groups; the separators are dropped, so 1,500,000 and ١٥٠٠٠٠٠ are one.
_NUMBER = re.compile(r"\d+(?:[.,\u066b\u066c]\d+)*")
_SEPARATORS = re.compile(r"[.,\u066b\u066c]")
# A run of letters, digits and the marks normalise() drops: one raw token.
_TOKEN = re.compile(r"[\w\u064b-\u065f\u0670\u0640]+")
# The one proclitic a word may carry in the quote or the segment and not the
# other, and the least the rest may be: a single letter is no word to match.
_PROCLITICS = frozenset("وفبل")
_MIN_BASE_LETTERS = 2
_DETACHED_AND = "و"


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Each quote word of `text` (module docstring) with the span of `text` it
    came from; a detached و joined to the word after it, spanning both."""
    found = [
        (word.translate(_QUOTE_FOLDS), token.start(), token.end())
        for token in _TOKEN.finditer(text)
        for word in words(token.group())
    ]
    joined: list[tuple[str, int, int]] = []
    at = 0
    while at < len(found):
        word, start, end = found[at]
        if word == _DETACHED_AND and at + 1 < len(found):
            following, _, end = found[at + 1]
            word, at = word + following, at + 1
        joined.append((word, start, end))
        at += 1
    return joined


def numbers_in(text: str) -> set[str]:
    """Every number written in `text` in digits, as ASCII digits alone."""
    folded = text.translate(_DIGITS)
    return {_SEPARATORS.sub("", found) for found in _NUMBER.findall(folded)}


def quote_words(text: str) -> list[str]:
    """The words a quote and a segment are matched by (module docstring)."""
    return [word for word, _, _ in _tokens(text)]


_NEGATION_WORDS = frozenset(quote_words(" ".join(NEGATIONS)))
_FILLER_WORDS = frozenset(quote_words(" ".join(QUOTE_FILLERS))) - _NEGATION_WORDS

# The reasons that doubt only who spoke, never what was said: the roles pass
# failed, or its mapping was not clear (roles.py).
ROLES_NOT_APPLIED = frozenset({"roles_failed", "roles_unclear"})

# A quote a pass needs said by one side, found in a voice no role mapping named.
SPEAKER_UNKNOWN = "quote_speaker_unknown"

# Where a quote not in its cited segment is looked for next: the segment just
# before, then the one just after.
_NEIGHBOURS = (-1, 1)
# A quote of at least this many words found word for word in no nearer
# segment is looked for in every other one, nearest first (D-102): the words
# were said, only the model's segment id was wrong. Lower lets "ok thanks" move
# to any "ok thanks"; higher refuses true quotes cited a few segments off.
FAR_MIN_WORDS = 4
# Why a refused quote was not found, logged as quote_miss (never the quote):
# the words stand in another segment; they stand only across two segments;
# at most this many of the quote's words are missing from the best nearby
# segment (altered_1 to altered_3, altered_3 meaning three or more); or fewer
# than half of them stand there at all (absent).
MISS_ALTERED_MAX = 3
_logger = logging.getLogger("dodeal_ai.unit_b")
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


class Answer(Strict):
    """An answer whose every field is required (a guard test holds it): a
    key it does not name is dropped unread, never a reprompt (D-101). Safe
    only because none has a default -- a misspelled key is then a missing
    field and still refused, never quietly read as a default."""

    model_config = ConfigDict(extra="ignore", frozen=True)


@dataclass(frozen=True, slots=True)
class CallText:
    """The transcript as every pass sees it: each segment's prompt copy and
    role, the summary language, whether the whole is uncertain, each side's
    language as the roles pass heard it, and whether the words themselves
    are uncertain (the whole is, for any reason but ROLES_NOT_APPLIED)."""

    segments: tuple[Segment, ...]
    shown: tuple[str, ...]
    language: SummaryLanguage
    uncertain: bool
    spoken: Spoken = UNHEARD
    words_uncertain: bool = True

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
            words_uncertain=is_uncertain(transcript.segments)
            or any(r not in ROLES_NOT_APPLIED for r in transcript.uncertain_reasons),
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
    repeats the word before it, or it is a filler (never a negation). A
    repeated negation follows its own matched copy, so none is ever lost."""
    return said[at] in _FILLER_WORDS or (at > 0 and said[at - 1] == said[at])


def _same(said: str, quoted: str) -> bool:
    """Whether a segment's word stands for a quote's: equal, or, neither a
    negation, one is the other with one proclitic in front."""
    if said == quoted:
        return True
    if said in _NEGATION_WORDS or quoted in _NEGATION_WORDS:
        return False
    longer, shorter = (said, quoted) if len(said) > len(quoted) else (quoted, said)
    return (
        len(shorter) >= _MIN_BASE_LETTERS
        and longer[0] in _PROCLITICS
        and longer[1:] == shorter
    )


def _reachable(said: Sequence[str], start: int) -> Iterator[int]:
    """The positions the next quote word may stand at after one that ended at
    `start`: `start`, and each after a run of skippable words."""
    at = start
    while at < len(said):
        yield at
        if not _skippable(said, at):
            return
        at += 1


def _span(said: Sequence[str], quote: Sequence[str]) -> tuple[int, int] | None:
    """The first and last positions of the earliest match of the quote's words
    in `said`, in order, nothing between two of them but skippable words
    (module docstring); None when there is none."""
    paths = {(at, at + 1) for at, word in enumerate(said) if _same(word, quote[0])}
    for wanted in quote[1:]:
        paths = {
            (first, at + 1)
            for first, start in paths
            for at in _reachable(said, start)
            if _same(said[at], wanted)
        }
    if not paths:
        return None
    first, end = min(paths)
    return first, end - 1


def _matches(said: Sequence[str], quote: Sequence[str]) -> bool:
    """Whether the quote's words stand in `said` (_span)."""
    return _span(said, quote) is not None


def _near(call: CallText, index: int) -> list[int]:
    """The cited segment's position, then its neighbours', in the call."""
    return [
        at
        for at in (index, *(index + step for step in _NEIGHBOURS))
        if 0 <= at < len(call.shown)
    ]


def _found_at(call: CallText, quoted: Sequence[str], index: int) -> int | None:
    """The segment the quote's words are in: the cited one, else the one just
    before, else the one just after; for a quote of FAR_MIN_WORDS or more,
    else any other, nearest first (D-102); None when none holds them."""
    near = _near(call, index)
    for at in near:
        if _matches(quote_words(call.shown[at]), quoted):
            return at
    if len(quoted) < FAR_MIN_WORDS:
        return None
    others = sorted(
        (at for at in range(len(call.shown)) if at not in near),
        key=lambda at: abs(at - index),
    )
    return next(
        (at for at in others if _matches(quote_words(call.shown[at]), quoted)), None
    )


def _common(said: Sequence[str], quote: Sequence[str]) -> int:
    """How many of the quote's words stand in `said` in order (the longest
    common subsequence under _same)."""
    row = [0] * (len(quote) + 1)
    for word in said:
        diagonal, row[0] = 0, 0
        for n, wanted in enumerate(quote, start=1):
            above = row[n]
            row[n] = diagonal + 1 if _same(word, wanted) else max(row[n], row[n - 1])
            diagonal = above
    return row[-1]


def miss_kind(call: CallText, quote: str | None, segment: str | None) -> str:
    """Why a refused quote was not found, as one fixed word (MISS_ALTERED_MAX's
    comment): elsewhere, joined, altered_N, absent -- or unchecked for one the
    quote check refused before looking (no segment, an unknown one, a length
    out of bounds). Never a word of the quote."""
    index = None if segment is None else call.index_of(segment)
    quoted = None if quote is None else _measured(quote)
    if index is None or quoted is None:
        return "unchecked"
    shown = [quote_words(text) for text in call.shown]
    if any(_matches(said, quoted) for said in shown):
        return "elsewhere"
    near = _near(call, index)
    if any(
        _matches(shown[at] + shown[at + 1], quoted)
        for at in near
        if at + 1 < len(shown)
    ):
        return "joined"
    best = max(_common(shown[at], quoted) for at in near)
    if best * 2 < len(quoted):
        return "absent"
    return f"altered_{min(len(quoted) - best, MISS_ALTERED_MAX)}"


def log_misses(
    label: str,
    call: CallText,
    errors: Iterable[tuple[str, str]],
    quote_at: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """One quote_miss line per quote_not_in_segment in `errors`: the label,
    where it is (a path code built), why it missed (miss_kind) and its word
    count. Codes and counts only, never the quote (D-102)."""
    for where, code in errors:
        if code != "quote_not_in_segment":
            continue
        quote, segment = quote_at(where)
        quoted = [] if quote is None else quote_words(quote)
        _logger.warning(
            "quote_miss label=%s where=%s kind=%s words=%d",
            label,
            where,
            miss_kind(call, quote, segment),
            len(quoted),
        )


def _measured(quote: str) -> list[str] | None:
    """The quote's words, or None when there are none or too many."""
    quoted = quote_words(quote)
    return quoted if quoted and len(quoted) <= MAX_QUOTE_WORDS else None


def holds(call: CallText, quote: str, index: int) -> bool:
    """Whether the segment at `index` holds the quote, under the quote check's
    length rule and matcher; no other segment is looked at."""
    quoted = _measured(quote)
    return quoted is not None and _matches(quote_words(call.shown[index]), quoted)


def own_words(call: CallText, quote: str, index: int) -> str | None:
    """The segment's own words the quote matched at `index`, first to last,
    as the prompt showed them; None when it does not hold the quote."""
    quoted = _measured(quote)
    tokens = _tokens(call.shown[index])
    span = None if quoted is None else _span([t[0] for t in tokens], quoted)
    if span is None:
        return None
    return one_line(call.shown[index][tokens[span[0]][1] : tokens[span[1]][2]])


def found_at(call: CallText, quote: str, segment: str) -> int | None:
    """The position of the segment a quote is found in (_found_at), or None
    when the cited id is unknown, the quote's length is wrong or no segment
    holds it."""
    index = call.index_of(segment)
    quoted = _measured(quote)
    if index is None or quoted is None:
        return None
    return _found_at(call, quoted, index)


def locate(call: CallText, quote: str, segment: str) -> str | None:
    """The id of the segment a quote is found in (found_at), or None."""
    found = found_at(call, quote, segment)
    return None if found is None else segment_id(found)


def relocated[M: BaseModel](call: CallText, answer: M) -> M:
    """The answer with every quote at the segment it is found in -- a quote
    found in a neighbour of the segment it cites is stored with the
    neighbour's id -- and as the transcript's own words it matched
    (own_words). Nothing else changes, and a failing quote stays as it is."""
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
            found = found_at(call, quote, segment)
            if found is None:
                continue
            if segment_id(found) != segment:
                update[segment_field] = segment_id(found)
            said = own_words(call, quote, found)
            if said is not None and said != quote:
                update[quote_field] = said
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
    quoted = _measured(quote)
    if quoted is None:
        return [(where, "quote_length")]
    found = _found_at(call, quoted, index)
    if found is None:
        return [(where, "quote_not_in_segment")]
    said = said_by(call.segments[found])
    if speaker is not None and said == UNKNOWN:
        return [(where, SPEAKER_UNKNOWN)]
    if speaker is not None and said != speaker:
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
    it is with its failures, failed; one failing only on an unknown voice is
    not counted failed."""
    failing = sum(
        1
        for errors in quotes.values()
        if any(code != SPEAKER_UNKNOWN for _, code in errors)
    )
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
