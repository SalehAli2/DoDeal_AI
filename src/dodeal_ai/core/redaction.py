"""Contact details out of the note text a model reads (register item 59).

`redact(text)` replaces emails with [EMAIL], phone-shaped digit runs with
[PHONE] and other long digit runs with [ID], in Western or Arabic-Indic digits.
Dates, times and short numbers (a price, a floor, a bedroom count) are kept,
because the rubric scores them. The pipeline fingerprints the ORIGINAL text and
redacts only what goes into a prompt's variable half.

THE RULES, in the order a match is tried at each position:
  date or time  kept when plausible (2026-01-15, 15/01/2026, 10:30)
  email         [EMAIL]
  digit run     digits joined by at most two of `+ space - . ( )`:
                with a separator or a leading +, 7+ digits is [PHONE];
                unformatted, 8+ digits is [ID]; anything shorter is kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Western, Arabic-Indic and Extended Arabic-Indic digits; notes mix them.
_D = "[0-9\u0660-\u0669\u06f0-\u06f9]"
_PHONE_MIN_DIGITS = 7
_ID_MIN_DIGITS = 8
_MONTHS = 12
_DAYS = 31
_HOURS = 23
_MINUTES = 59

_PATTERN = re.compile(
    rf"(?P<date>{_D}{{4}}[-/.]{_D}{{1,2}}[-/.]{_D}{{1,2}}(?!{_D})"
    rf"|{_D}{{1,2}}[-/.]{_D}{{1,2}}[-/.]{_D}{{2,4}}(?!{_D}))"
    rf"|(?P<time>{_D}{{1,2}}:{_D}{{2}}(?::{_D}{{2}})?(?!{_D}))"
    r"|(?P<email>[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
    rf"|(?P<run>\+?\(?{_D}(?:[ .()\-]{{1,2}}{_D}|{_D})*)"
)
_DIGIT = re.compile(_D)
_PARTS = re.compile(rf"{_D}+")


@dataclass(frozen=True, slots=True)
class Redaction:
    """The redacted text and how many of each kind were replaced. The counts are
    safe on a log line; the text is note-derived and never is."""

    text: str
    phone: int = 0
    email: int = 0
    id: int = 0

    def fields(self) -> dict[str, int]:
        """The three counts, named as the outcome line carries them."""
        return {
            "redacted_phone": self.phone,
            "redacted_email": self.email,
            "redacted_id": self.id,
        }


def redact(text: str) -> Redaction:
    """`text` with emails, phones and long ids replaced, and the three counts."""
    counts = {"phone": 0, "email": 0, "id": 0}

    def _replace(match: re.Match[str]) -> str:
        found = match.group(0)
        if match.group("email") is not None:
            counts["email"] += 1
            return "[EMAIL]"
        if match.group("date") is not None and _plausible_date(found):
            return found
        if match.group("time") is not None and _plausible_time(found):
            return found
        kind = _run_kind(found)
        if kind is None:
            return found
        counts[kind] += 1
        return f"[{kind.upper()}]"

    redacted = _PATTERN.sub(_replace, text)
    return Redaction(redacted, counts["phone"], counts["email"], counts["id"])


def _run_kind(run: str) -> str | None:
    """Classify one digit run as phone, id, or None to keep it."""
    digits = len(_DIGIT.findall(run))
    formatted = len(_PARTS.findall(run)) > 1 or run.startswith(("+", "("))
    if formatted:
        return "phone" if digits >= _PHONE_MIN_DIGITS else None
    return "id" if digits >= _ID_MIN_DIGITS else None


def _numbers(text: str) -> list[int]:
    """Each digit group as an int; int() reads Arabic-Indic digits too."""
    return [int(part) for part in _PARTS.findall(text)]


def _plausible_date(text: str) -> bool:
    """A real calendar shape: year-first (month, day), or year-last with the day
    and month in either order, so 15/01/2026 and 01/15/2026 both stay."""
    first, second, third = _numbers(text)
    if len(_PARTS.findall(text)[0]) == 4:
        return _in_range(month=second, day=third)
    return _in_range(month=second, day=first) or _in_range(month=first, day=second)


def _in_range(*, month: int, day: int) -> bool:
    return 1 <= month <= _MONTHS and 1 <= day <= _DAYS


def _plausible_time(text: str) -> bool:
    """Hours then minutes (then seconds), each in range."""
    hours, minutes, *seconds = _numbers(text)
    return (
        hours <= _HOURS and minutes <= _MINUTES and all(s <= _MINUTES for s in seconds)
    )
