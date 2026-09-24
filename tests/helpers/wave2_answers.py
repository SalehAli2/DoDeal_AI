"""Well-formed wave 2 answers for tests that run the whole wave but test one
pass: each quotes at most what the call's first segment says, so it holds on
any invented English call."""

from __future__ import annotations

from typing import Any


def extras_answer() -> dict[str, Any]:
    """A unit_b.extras answer that quotes nothing: every check a no."""
    check = {"answer": "no", "reason": "Not said on the call.", "quote": None}
    return {
        "keywords": [],
        "tags": {
            "outcome": "needs_follow_up",
            "stage": "first_contact",
            "client_type": "unknown",
        },
        "whatsapp": "Thank you for your time today. When suits you for a call?",
        "seriousness": {
            name: {**check, "segment": None}
            for name in (
                "budget_stated",
                "timeline_stated",
                "decision_maker_named",
                "next_step_agreed",
                "client_engaged",
            )
        },
    }


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
