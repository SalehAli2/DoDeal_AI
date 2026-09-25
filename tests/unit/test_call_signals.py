"""Call signals (units/call_intelligence/signals.py): scripted transcripts give
the expected talk shares, words per minute, interruptions and talk balance,
and none of them while the roles are not applied."""

from __future__ import annotations

import pytest

from dodeal_ai.units.call_intelligence.signals import (
    call_signals,
    interruptions,
    talk_balance,
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
        "version": "call_signals_v2",
        "agent": {"talk_share": 0.75, "words_per_minute": 30.0, "interruptions": 1},
        "client": {
            "talk_share": 0.25,
            "words_per_minute": 60.0,
            "interruptions": 1,
        },
        "talk_balance": "agent_heavy",
        "talk_reason": None,
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


# --- the guard: no roles, no talk signals ------------------------------------------

_NO_TALK = {"talk_share": None, "words_per_minute": None, "interruptions": None}


@pytest.mark.parametrize(
    "labels",
    [("speaker_1", "speaker_2"), ("unknown", "unknown"), ("agent", "unknown")],
)
def test_with_roles_not_applied_every_talk_signal_is_null(
    labels: tuple[str, str],
) -> None:
    first, second = labels
    script = (
        _say(0, 7, first, "one two three and"),
        _say(7.1, 10, second, "wait four five."),
    )
    assert call_signals(script) == {
        "version": "call_signals_v2",
        "agent": _NO_TALK,
        "client": _NO_TALK,
        "talk_balance": None,
        "talk_reason": "roles_not_applied",
    }


# --- talk balance ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("share", "balance"),
    [
        (0.70, "agent_heavy"),
        (0.50, "balanced"),
        (0.66, "agent_heavy"),
        (0.65, "balanced"),
        (0.35, "balanced"),
        (0.34, "client_led"),
        (0.0, "client_led"),
        (None, None),
    ],
)
def test_talk_balance_reads_the_agents_share(
    share: float | None, balance: str | None
) -> None:
    assert talk_balance(share) == balance


def test_an_even_call_is_balanced_beside_its_numbers() -> None:
    script = (_say(0, 5, "agent", "hello there."), _say(5.5, 10.5, "lead", "hi."))
    signals = call_signals(script)
    assert (signals["agent"], signals["talk_balance"], signals["talk_reason"]) == (
        {"talk_share": 0.5, "words_per_minute": 24.0, "interruptions": 0},
        "balanced",
        None,
    )


def test_no_speech_has_no_balance() -> None:
    signals = call_signals(())
    assert (signals["talk_balance"], signals["talk_reason"]) == (None, None)
