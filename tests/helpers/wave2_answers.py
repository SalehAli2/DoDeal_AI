"""Well-formed wave 2 answers for tests that run the whole wave but test one
pass: each quotes only what the call's first segment says, so it holds on any
invented English call."""

from __future__ import annotations

from typing import Any


def coaching_answer(quote: str) -> dict[str, Any]:
    """A unit_b.coaching answer resting on segment s1, which says `quote`."""
    stage = {"done": "no", "quote": None, "segment": None}
    return {
        "observations": [
            {
                "kind": "strength",
                "text": "Opened the call politely.",
                "quote": quote,
                "segment": "s1",
                "say_it_like_this": None,
            },
            {
                "kind": "improvement",
                "text": "Could ask about the client's needs sooner.",
                "quote": quote,
                "segment": "s1",
                "say_it_like_this": "What matters most to you in a new home?",
            },
        ],
        "moments": [],
        "plan": [
            "Ask about budget early.",
            "Offer two viewing slots.",
            "Confirm the next step aloud.",
        ],
        "stages": {
            name: dict(stage)
            for name in (
                "opening",
                "rapport",
                "discovery",
                "qualification",
                "presentation",
                "objections",
                "close",
            )
        },
    }
