"""The summary language (units/call_intelligence/language.py): one row per case
of the table, mixed decided by words and a tie going to Arabic."""

from __future__ import annotations

import pytest

from dodeal_ai.units.call_intelligence.language import summary_language
from dodeal_ai.units.call_intelligence.transcriber import (
    LanguageProfile,
    Segment,
    Transcript,
)


def _segment(start: float, seconds: float, language: str, words: int) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + seconds,
        speaker="agent",
        text=" ".join(["word"] * words),
        language=language,
        confidence=0.9,
    )


def _transcript(*parts: tuple[str, float, int]) -> Transcript:
    """One segment per (language, seconds, words), back to back."""
    segments, start = [], 0.0
    for language, seconds, words in parts:
        segments.append(_segment(start, seconds, language, words))
        start += seconds
    return Transcript.of(tuple(segments), provider="fake", model="fake-stt-1")


@pytest.mark.parametrize(
    ("parts", "profile", "language"),
    [
        ((("ar", 90, 10), ("en", 10, 40)), LanguageProfile.MOSTLY_AR, "ar"),
        ((("en", 90, 10), ("ar", 10, 40)), LanguageProfile.MOSTLY_EN, "en"),
        ((("en", 30, 20), ("ar", 70, 15)), LanguageProfile.MIXED, "en"),
        ((("en", 70, 15), ("ar", 30, 20)), LanguageProfile.MIXED, "ar"),
        ((("en", 50, 12), ("ar", 50, 12)), LanguageProfile.MIXED, "ar"),
        ((("fr", 60, 30), ("en", 40, 5)), LanguageProfile.OTHER, "en"),
    ],
    ids=[
        "mostly-ar",
        "mostly-en",
        "mixed-more-english-words",
        "mixed-more-arabic-words",
        "mixed-tie",
        "other",
    ],
)
def test_the_summary_language_follows_the_table(
    parts: tuple, profile: LanguageProfile, language: str
) -> None:
    transcript = _transcript(*parts)
    assert transcript.language_profile is profile
    assert summary_language(transcript) == language


def test_words_in_a_third_language_count_for_neither() -> None:
    transcript = _transcript(("en", 45, 5), ("ar", 45, 6), ("fr", 10, 100))
    assert transcript.language_profile is LanguageProfile.MIXED
    assert summary_language(transcript) == "ar"
