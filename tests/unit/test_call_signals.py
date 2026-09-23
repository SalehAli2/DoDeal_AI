"""Call signals (units/call_intelligence/signals.py): scripted transcripts give
the expected talk shares, words per minute and interruptions."""

from __future__ import annotations

from dodeal_ai.units.call_intelligence.signals import (
    SIGNALS_VERSION,
    call_signals,
    interruptions,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment


def _say(start: float, end: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=end,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


# 30 s of agent in 15 words, 10 s of client in 10 words; three quick turns.
SCRIPT = (
    _say(0, 20, "agent", "one two three four five six seven eight nine ten"),
    # 0.1 s after an unfinished sentence: an interruption by the client.
    _say(20.1, 25.1, "lead", "wait one two three four"),
    # 0.1 s after an unfinished one again: the agent interrupts back.
    _say(25.2, 35.2, "agent", "one two three four five."),
    # 0.1 s after a finished sentence: a quick reply, not an interruption.
    _say(35.3, 40.3, "lead", "yes fine thanks all good."),
)


def test_a_scripted_call_gives_the_expected_numbers() -> None:
    assert call_signals(SCRIPT) == {
        "version": SIGNALS_VERSION,
        "agent": {"talk_share": 0.75, "words_per_minute": 30.0, "interruptions": 1},
        "client": {
            "talk_share": 0.25,
            "words_per_minute": 60.0,
            "interruptions": 1,
        },
    }


def test_a_turn_at_the_gap_or_after_a_question_is_not_an_interruption() -> None:
    script = (
        _say(0, 5, "agent", "and the price is"),
        _say(5.3, 8, "lead", "too high."),
        _say(8.1, 10, "agent", "is it?"),
        _say(10.05, 12, "lead", "نعم؟"),
        _say(12.1, 14, "agent", "حسناً"),
    )
    assert interruptions(script) == {"agent": 0, "client": 0}


def test_one_speaker_continuing_never_interrupts_themselves() -> None:
    script = (_say(0, 5, "agent", "and so"), _say(5.05, 9, "agent", "we go on"))
    assert interruptions(script) == {"agent": 0, "client": 0}


def test_a_role_that_never_spoke_has_no_rate() -> None:
    signals = call_signals((_say(0, 30, "agent", "hello there"),))
    assert signals["client"] == {
        "talk_share": 0.0,
        "words_per_minute": None,
        "interruptions": 0,
    }


def test_no_speech_has_no_shares() -> None:
    signals = call_signals(())
    assert signals["agent"] == {
        "talk_share": None,
        "words_per_minute": None,
        "interruptions": 0,
    }
