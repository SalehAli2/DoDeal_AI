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
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from dodeal_ai.units.call_intelligence.prompts import AGENT, CLIENT, role_of
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


def call_signals(segments: Sequence[Segment]) -> dict[str, object]:
    """The signals block stage 1 carries: its version, then one entry a role."""
    total = sum(segment.seconds for segment in segments)
    cut_ins = interruptions(segments)
    block: dict[str, object] = {"version": SIGNALS_VERSION}
    for role in (AGENT, CLIENT):
        spoken = [segment for segment in segments if role_of(segment) == role]
        seconds = sum(segment.seconds for segment in spoken)
        words = sum(len(segment.text.split()) for segment in spoken)
        block[role] = {
            "talk_share": round(seconds / total, _SHARE_PLACES) if total else None,
            "words_per_minute": (
                round(words / (seconds / _SECONDS_PER_MINUTE), _RATE_PLACES)
                if seconds
                else None
            ),
            "interruptions": cut_ins[role],
        }
    return block
