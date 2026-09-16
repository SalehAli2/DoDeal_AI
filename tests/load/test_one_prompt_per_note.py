"""One question per note under a burst (register item 119).

Both requests are parked in classify until each has read the note's attempt
count, so they meet at the prompt-slots script having both read 0: the race the
old read-then-increment lost, made certain rather than left to scheduling.
"""

from __future__ import annotations

import asyncio

import pytest

from dodeal_ai.units.structured_intelligence.state import (
    _attempt_key,
    _rate_limit_key,
)
from tests.load.conftest import SUBJECT, TENANT

# How long both requests may take to reach classify, and how often to look.
PARK_SECONDS = 5.0
POLL_SECONDS = 0.005


async def test_one_note_id_with_two_vague_texts_at_once_sends_one_prompt(lane):
    """The same note id with two different vague texts at once on the direct route gives one prompt_sent, one attempt_cap, and one attempt and one rate slot counted."""
    lead_id, note = lane.notes[0]
    edited = f"{note.note} Viewing confirmed for Tuesday at noon."
    lane.script(judgements=2)
    lane.llm.hold_after = 0

    tasks = [
        asyncio.create_task(lane.judge_direct(lead_id, note, text))
        for text in (note.note, edited)
    ]
    try:
        async with asyncio.timeout(PARK_SECONDS):
            while lane.llm.call_count < len(tasks):
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        pytest.fail(f"{lane.llm.call_count} of 2 judgements reached classify in time")
    finally:
        lane.llm.released.set()
    responses = await asyncio.gather(*tasks)

    assert [r.status_code for r in responses] == [200, 200]
    decisions = [r.json()["decision"] for r in responses]
    assert sorted(d["prompt_sent"] for d in decisions) == [False, True]
    withheld = next(d for d in decisions if not d["prompt_sent"])
    assert withheld["prompt_withheld"] == "attempt_cap"
    operational = lane.stores.operational
    assert await operational.get(_attempt_key(TENANT, lead_id, note.id)) == "1"
    assert await operational.get(_rate_limit_key(TENANT, str(SUBJECT))) == "1"
