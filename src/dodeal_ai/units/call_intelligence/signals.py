"""Call signals (call_signals_v1): numbers read from the segments' timing and
words, per speaker, with no model.

  talk_share        the speaker's share of all spoken seconds, 0 to 1
  words_per_minute  the speaker's words over the speaker's spoken minutes
  interruptions     turns the speaker took under the provisional rule below

THE INTERRUPTION RULE IS PROVISIONAL, until calls with known interruptions
exist to tune it on: a speaker interrupts when their segment starts less than
0.3 s after the other speaker's segment ends AND that segment has no terminal
punctuation (. ! ? ... and the Arabic question mark and full stop). A quick
reply to a finished sentence is not an interruption; a cut-in on an
unfinished one is. Segments never overlap (transcriber.py), so the gap is
never negative, and a transcriber that punctuates nothing makes every quick
turn an interruption: read these numbers beside the transcriber's name.

Per speaker means per role, agent and client (prompts.py::role_of). A role
that never spoke has no rate: its words_per_minute is None, not 0.

WHILE THE ROLES ARE NOT APPLIED -- any segment's speaker unknown
(prompts.said_by) -- no one is known to be the agent, so every talk signal,
agent and client, is None and talk_reason is roles_not_applied.

talk_balance reads the agent's reported talk_share: client_led below
CLIENT_LED_BELOW, agent_heavy above AGENT_HEAVY_ABOVE, balanced between them,
both ends included; None when the share is.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    CLIENT,
    UNKNOWN,
    role_of,
    said_by,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment

SIGNALS_VERSION = "call_signals_v1"

# A turn starting sooner than this after the other speaker stopped mid-sentence
# is an interruption (provisional; see the module docstring).
INTERRUPTION_GAP_SECONDS = 0.3

# What ends a sentence, in the scripts calls are spoken in.
_TERMINAL = (".", "!", "?", "…", "؟", "۔")

# Decimal places kept, so the payload does not carry float noise.
_SHARE_PLACES = 3
_RATE_PLACES = 1
# A gap is read to the millisecond, so 5.3 - 5.0 is 0.3 and not 0.2999...
_GAP_PLACES = 3

_SECONDS_PER_MINUTE = 60

# Why the talk signals are None, a fixed code (module docstring).
ROLES_NOT_APPLIED = "roles_not_applied"

# talk_balance's bands over the agent's talk share. Provisional, like the
# interruption rule: a third either side of an even split. A wrong value
# labels an ordinary call agent_heavy or client_led.
CLIENT_LED_BELOW = 0.35
AGENT_HEAVY_ABOVE = 0.65
CLIENT_LED = "client_led"
BALANCED = "balanced"
AGENT_HEAVY = "agent_heavy"


def _ends_a_sentence(text: str) -> bool:
    return text.rstrip().endswith(_TERMINAL)


def interruptions(segments: Sequence[Segment]) -> dict[str, int]:
    """Each role's interruptions, by the provisional rule."""
    counts = {AGENT: 0, CLIENT: 0}
    for before, after in pairwise(segments):
        role = role_of(after)
        if role == role_of(before):
            continue
        gap = round(after.start_s - before.end_s, _GAP_PLACES)
        quick = gap < INTERRUPTION_GAP_SECONDS
        if quick and not _ends_a_sentence(before.text):
            counts[role] += 1
    return counts


def talk_share(segments: Sequence[Segment], role: str) -> float | None:
    """The role's share of all spoken seconds, as stage 1 reports it; None
    when nobody spoke."""
    total = sum(segment.seconds for segment in segments)
    if not total:
        return None
    seconds = sum(s.seconds for s in segments if role_of(s) == role)
    return round(seconds / total, _SHARE_PLACES)


def talk_balance(agent_share: float | None) -> str | None:
    """Which side led the talk, by the agent's share; None without one."""
    if agent_share is None:
        return None
    if agent_share < CLIENT_LED_BELOW:
        return CLIENT_LED
    if agent_share > AGENT_HEAVY_ABOVE:
        return AGENT_HEAVY
    return BALANCED


def roles_applied(segments: Sequence[Segment]) -> bool:
    """Whether every segment's speaker is a role, none unknown."""
    return all(said_by(segment) != UNKNOWN for segment in segments)


def call_signals(segments: Sequence[Segment]) -> dict[str, object]:
    """The signals block stage 1 carries: its version, one entry a role, the
    talk balance and why the talk signals are None, when they are."""
    if not roles_applied(segments):
        unknown = dict.fromkeys(("talk_share", "words_per_minute", "interruptions"))
        return {
            "version": SIGNALS_VERSION,
            AGENT: unknown,
            CLIENT: dict(unknown),
            "talk_balance": None,
            "talk_reason": ROLES_NOT_APPLIED,
        }
    cut_ins = interruptions(segments)
    block: dict[str, object] = {"version": SIGNALS_VERSION}
    for role in (AGENT, CLIENT):
        spoken = [segment for segment in segments if role_of(segment) == role]
        seconds = sum(segment.seconds for segment in spoken)
        words = sum(len(segment.text.split()) for segment in spoken)
        block[role] = {
            "talk_share": talk_share(segments, role),
            "words_per_minute": (
                round(words / (seconds / _SECONDS_PER_MINUTE), _RATE_PLACES)
                if seconds
                else None
            ),
            "interruptions": cut_ins[role],
        }
    block["talk_balance"] = talk_balance(talk_share(segments, AGENT))
    block["talk_reason"] = None
    return block
