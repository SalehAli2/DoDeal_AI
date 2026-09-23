"""The language a call's summary and CRM note are written in, decided in code
from the transcript and never by a model.

  mostly_ar  ar
  mostly_en  en
  mixed      whichever of Arabic and English has more words, counted over the
             segments spoken in it; a tie is ar
  other      en

The profile is the transcript's own (transcriber.py, by spoken time); only a
mixed call looks further, at words, because time favours whoever speaks
slowly. A word is a whitespace-separated token of a segment's text.
"""

from __future__ import annotations

from typing import Literal

from dodeal_ai.units.call_intelligence.transcriber import LanguageProfile, Transcript

type SummaryLanguage = Literal["ar", "en"]


def summary_language(transcript: Transcript) -> SummaryLanguage:
    """ar or en for this transcript, by the table in the module docstring."""
    profile = transcript.language_profile
    if profile is LanguageProfile.MOSTLY_AR:
        return "ar"
    if profile is not LanguageProfile.MIXED:
        return "en"
    words = {
        code: sum(
            len(segment.text.split())
            for segment in transcript.segments
            if segment.language == code
        )
        for code in ("ar", "en")
    }
    return "en" if words["en"] > words["ar"] else "ar"
