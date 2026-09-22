"""Which script a note is written in -- the one rule, in one place (register
item 34).

Three buckets decided by script, not by a language model: any Arabic-script
letter and any Latin letter is `mixed`, Arabic alone is `arabic`, and
everything else -- Latin alone, or neither script at all -- is `english`, the
residual bucket. It is a fact about the characters, stamped on every judgement
as `analysis.language`, and read by the thin note's fixed question and by the
eval runner's per-language figures. It changes no mark and no decision.

Read on the note's OWN text, never the redacted copy: redaction replaces
digits and contact details, which could only ever move a note toward
`english`.
"""

from __future__ import annotations

import re
from enum import StrEnum

# Any letter in the Arabic block. Covers Arabic-script Persian and Urdu too,
# which this service does not tell apart; the bucket is the script.
ARABIC_SCRIPT_PATTERN = re.compile("[؀-ۿ]")

# Latin letters only. A wrong value mislabels a bucket and changes no judgement.
LATIN_SCRIPT_PATTERN = re.compile("[A-Za-z]")


class Language(StrEnum):
    """The three buckets, as `analysis.language` carries them."""

    ARABIC = "arabic"
    ENGLISH = "english"
    MIXED = "mixed"


def has_arabic(text: str) -> bool:
    """Does the text contain any Arabic-script letter?"""
    return ARABIC_SCRIPT_PATTERN.search(text) is not None


def language_of(text: str) -> Language:
    """arabic, english or mixed, decided by script."""
    arabic = has_arabic(text)
    latin = LATIN_SCRIPT_PATTERN.search(text) is not None
    if arabic and latin:
        return Language.MIXED
    return Language.ARABIC if arabic else Language.ENGLISH
