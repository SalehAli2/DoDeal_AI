"""Phone numbers said on a call (Unit B): found in code, compared with the
CRM's two hashes by SHA-256, and masked out of what a model reads.

WHAT IS A NUMBER. A chain of digit tokens with only separators between them
(spaces, commas, full stops, hyphens, parentheses, the Arabic comma): digit
groups in Western or Arabic-Indic digits, and digit words in English or
Arabic, Gulf, Levantine and Egyptian forms included ("zero five oh", "صفر
خمسة", "اتنين", "تمانية"). "double" and "triple" repeat the next digit; "plus"
or "زائد" first stands for +. A chain may hold more than one number, so the
longest phone-shaped stretch is taken from each place in it, left to right.

THE PHONE RULE, and the rule the CRM must hash with:

  1. every digit as an ASCII digit, every separator dropped;
  2. a leading + or 00 is dropped (+971 and 00971 both become 971);
  3. otherwise a leading 05 becomes 9715;
  4. it is a phone number when it had a + or 00 and is 8 to 15 digits, or
     starts with 971 and is 11 or 12, or starts with 0 and is 9 to 11.

Anything else -- a price, a year, a unit number -- has no leading 0, + or 971
and is left alone: "1,200,000 AED" is not a phone number and is not masked.

WHAT A FIND CARRIES: who said it (agent or client), when, which segment, the
last four digits and what it matched -- never the number or its hash. The
lead's hash is `lead`, the agent's is `agent_company`; any other number is
`new_client_number` from the client and `agent_personal` from the agent, and
an agent_personal number is an off_channel_contact escalation. With no hash
on the push nothing can match it: an agent's every number is then personal.

THE PROMPT COPY masks every phone number as [PHONE] and every email address as
[EMAIL]; the stored transcript keeps what was said.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from dodeal_ai.units.call_intelligence.prompts import AGENT, role_of, segment_id
from dodeal_ai.units.call_intelligence.transcriber import Segment

PHONE_MASK = "[PHONE]"
EMAIL_MASK = "[EMAIL]"
OFF_CHANNEL = "off_channel_contact"

# What a number said on the call matched.
MATCH_LEAD = "lead"
MATCH_AGENT_COMPANY = "agent_company"
MATCH_NEW_CLIENT = "new_client_number"
MATCH_AGENT_PERSONAL = "agent_personal"

# The phone rule's lengths (module docstring, step 4). E.164 caps a number at
# 15 digits; the shortest international number in use is 8.
_INTERNATIONAL = range(8, 16)
_UAE = range(11, 13)
_LOCAL = range(9, 12)
_UAE_CODE = "971"
_LAST_DIGITS = 4

# A token is a maximal stretch without a separator.
_TOKEN = re.compile(r"[^\s,.\-،]+")
# A written digit group, with a leading + or ( and a trailing ) allowed.
_GROUP = re.compile(r"(?P<plus>\+)?\(?(?P<digits>[0-9٠-٩۰-۹]+)\)?")
# Arabic diacritics and the tatweel, which a digit word may carry.
_MARKS = re.compile(r"[\u064b-\u065f\u0670\u0640]")
_ARABIC_INDIC = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

# Every spelling of a digit a call may carry: English, then Arabic in its
# standard, Gulf, Levantine and Egyptian forms, hamza and ta marbuta variants
# spelled out.
_DIGIT_WORDS: dict[str, str] = {
    **dict.fromkeys(("zero", "oh", "o"), "0"),
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    **dict.fromkeys(("صفر", "زيرو"), "0"),
    **dict.fromkeys(("واحد", "وحده", "وحدة"), "1"),
    **dict.fromkeys(
        ("اثنين", "إثنين", "اثنان", "إثنان", "اثنتين", "اتنين", "ثنين", "تنين"),
        "2",
    ),
    **dict.fromkeys(("ثلاثة", "ثلاثه", "ثلاث", "تلاتة", "تلاته", "تلات"), "3"),
    **dict.fromkeys(("أربعة", "اربعة", "أربعه", "اربعه", "أربع", "اربع"), "4"),
    **dict.fromkeys(("خمسة", "خمسه", "خمس"), "5"),
    **dict.fromkeys(("ستة", "سته", "ست"), "6"),
    **dict.fromkeys(("سبعة", "سبعه", "سبع"), "7"),
    **dict.fromkeys(
        ("ثمانية", "ثمانيه", "ثماني", "ثمان", "تمانية", "تمانيه", "تمنية", "تمان"),
        "8",
    ),
    **dict.fromkeys(("تسعة", "تسعه", "تسع"), "9"),
}
_REPEATS = {"double": 2, "دبل": 2, "triple": 3, "تريبل": 3}
_PLUS_WORDS = frozenset({"plus", "زائد", "زايد"})
_AND = "و"


@dataclass(frozen=True, slots=True)
class PhoneSpan:
    """One phone number in a text: its character span, and the digits the
    rule makes of it. The digits never leave this module but as a hash."""

    start: int
    end: int
    digits: str


@dataclass(frozen=True, slots=True)
class _Piece:
    """One digit-bearing token of a chain: where it is and what it says."""

    start: int
    end: int
    digits: str


def phone_digits(digits: str, *, plus: bool) -> str | None:
    """The phone rule (module docstring): the digits to hash, or None when
    these digits are not a phone number."""
    if plus or digits.startswith("00"):
        number = digits if plus else digits[2:]
        return number if len(number) in _INTERNATIONAL else None
    if digits.startswith(_UAE_CODE):
        return digits if len(digits) in _UAE else None
    if digits.startswith("0") and len(digits) in _LOCAL:
        return f"{_UAE_CODE}{digits[1:]}" if digits.startswith("05") else digits
    return None


def phone_hash(digits: str) -> str:
    """SHA-256 hex of the rule's digits, as the CRM hashes a number."""
    return hashlib.sha256(digits.encode("ascii")).hexdigest()


def _word(token: str) -> str:
    return _MARKS.sub("", token).casefold()


def _digit_word(token: str) -> str | None:
    """The digit a word says, `و` ("and") in front of it allowed."""
    word = _word(token)
    if word in _DIGIT_WORDS:
        return _DIGIT_WORDS[word]
    if word.startswith(_AND):
        return _DIGIT_WORDS.get(word[len(_AND) :])
    return None


def _chains(text: str) -> list[tuple[int | None, list[_Piece]]]:
    """Every run of digit tokens in `text`, each with where the + that led
    it starts, or None when no + did."""
    chains: list[tuple[int | None, list[_Piece]]] = []
    pieces: list[_Piece] = []
    plus: int | None = None
    repeat: tuple[int, int] | None = None

    def close() -> None:
        nonlocal pieces, plus, repeat
        if pieces:
            chains.append((plus, pieces))
        pieces, plus, repeat = [], None, None

    for token in _TOKEN.finditer(text):
        word = _word(token.group())
        group = _GROUP.fullmatch(token.group())
        digit = _digit_word(token.group())
        if group is not None:
            if group.group("plus"):
                close()
                plus = token.start()
            digits = group.group("digits").translate(_ARABIC_INDIC)
            pieces.append(_Piece(token.start(), token.end(), digits))
            repeat = None
        elif digit is not None:
            times, start = repeat or (1, token.start())
            pieces.append(_Piece(start, token.end(), digit * times))
            repeat = None
        elif word in _REPEATS:
            repeat = (_REPEATS[word], token.start())
        elif word in _PLUS_WORDS:
            close()
            plus = token.start()
        else:
            close()
    close()
    return chains


def phone_spans(text: str) -> list[PhoneSpan]:
    """Every phone number in `text`, left to right, never overlapping."""
    found: list[PhoneSpan] = []
    for plus, pieces in _chains(text):
        first = 0
        while first < len(pieces):
            led = plus if first == 0 else None
            span = _longest_phone(pieces, first, plus=led)
            if span is None:
                first += 1
                continue
            found.append(span[0])
            first = span[1] + 1
    return found


def _longest_phone(
    pieces: list[_Piece], first: int, *, plus: int | None
) -> tuple[PhoneSpan, int] | None:
    """The longest phone number starting at pieces[first] -- at the + that led
    it, when one did -- and its last piece."""
    start = pieces[first].start if plus is None else plus
    for last in range(len(pieces) - 1, first - 1, -1):
        digits = "".join(piece.digits for piece in pieces[first : last + 1])
        number = phone_digits(digits, plus=plus is not None)
        if number is not None:
            return PhoneSpan(start, pieces[last].end, number), last
    return None


def prompt_copy(text: str) -> str:
    """`text` as a model may read it: emails and phone numbers masked,
    everything else -- prices among it -- as said."""
    masked = _EMAIL.sub(EMAIL_MASK, text)
    for span in reversed(phone_spans(masked)):
        masked = f"{masked[: span.start]}{PHONE_MASK}{masked[span.end :]}"
    return masked


@dataclass(frozen=True, slots=True)
class NumberFindings:
    """What number detection found: the finds and the escalations they raise."""

    finds: list[dict[str, object]]
    escalations: list[dict[str, object]]


def _match(digest: str, role: str, lead: str | None, agent: str | None) -> str:
    if lead is not None and digest == lead:
        return MATCH_LEAD
    if agent is not None and digest == agent:
        return MATCH_AGENT_COMPANY
    return MATCH_AGENT_PERSONAL if role == AGENT else MATCH_NEW_CLIENT


def detect_numbers(
    segments: Sequence[Segment],
    *,
    lead_phone_hash: str | None,
    agent_phone_hash: str | None,
) -> NumberFindings:
    """Every phone number said, matched against the two hashes."""
    finds: list[dict[str, object]] = []
    escalations: list[dict[str, object]] = []
    lead = lead_phone_hash.lower() if lead_phone_hash else None
    agent = agent_phone_hash.lower() if agent_phone_hash else None
    for index, segment in enumerate(segments):
        role = role_of(segment)
        for span in phone_spans(_EMAIL.sub(EMAIL_MASK, segment.text)):
            match = _match(phone_hash(span.digits), role, lead, agent)
            where = {
                "speaker": role,
                "start_s": segment.start_s,
                "segment": segment_id(index),
            }
            finds.append(
                {**where, "last4": span.digits[-_LAST_DIGITS:], "match": match}
            )
            if match == MATCH_AGENT_PERSONAL:
                escalations.append({"type": OFF_CHANNEL, "source": "number", **where})
    return NumberFindings(finds, escalations)


def numbers_if_enabled(
    segments: Sequence[Segment],
    *,
    enabled: bool,
    lead_phone_hash: str | None,
    agent_phone_hash: str | None,
) -> NumberFindings | None:
    """detect_numbers under the tenant's number_detection_enabled switch; None,
    and nothing looked for, while it is off."""
    if not enabled:
        return None
    return detect_numbers(
        segments, lead_phone_hash=lead_phone_hash, agent_phone_hash=agent_phone_hash
    )
