"""Load scenarios 1 to 4: concurrent bursts through the served app on real Redis.

Each burst is `asyncio.gather` over real requests on one event loop, so the
reservation, the cost script and the rate-limit script meet genuine interleaving
on a real server rather than a fake's dict. Scenario 6 is in its own module,
because it serves the app on an operational store that is not there.
"""

from __future__ import annotations

import asyncio

# Scenario 2's burst. The corpus supplies 18 leads with a note the length gate
# passes; the test asserts it still supplies this many rather than testing fewer.
S2_BURST = 15


async def test_s1_two_identical_requests_at_once_are_one_judgement(lane):
    """Two identical requests at the same instant give one 200 and one 409, with 3 model calls, not 6."""
    lead_id, note = lane.notes[0]
    # Two judgements' worth: a lost reservation shows as 6 calls and two 200s,
    # not as an exhausted script.
    lane.script(judgements=2)

    responses = await asyncio.gather(
        lane.judge(lead_id, note.id), lane.judge(lead_id, note.id)
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
    duplicate = next(r for r in responses if r.status_code == 409)
    assert duplicate.json()["reason"] == "duplicate_request"
    assert lane.llm.call_count == 3


async def test_s2_fifteen_notes_from_one_subject_at_once_count_exactly_fifteen(lane):
    """Fifteen different notes on different leads at once all return 200 for their own note, and the per-user counter reads exactly 15."""
    picked = lane.notes[:S2_BURST]
    assert len(picked) == S2_BURST
    assert len({lead_id for lead_id, _ in picked}) == S2_BURST
    lane.script(judgements=S2_BURST)

    responses = await asyncio.gather(
        *(lane.judge(lead_id, note.id) for lead_id, note in picked)
    )

    assert [r.status_code for r in responses] == [200] * S2_BURST
    assert [r.json()["note_id"] for r in responses] == [note.id for _, note in picked]
    assert await lane.stores.cost.get(lane.user_cost_key) == str(S2_BURST)


async def test_s3_four_vague_notes_in_one_burst_send_three_prompts(lane):
    """Four vague notes from one subject at once all return 200, three send a prompt and one is withheld as rate_limited."""
    picked = lane.notes[:4]
    lane.script(judgements=4)

    responses = await asyncio.gather(
        *(lane.judge(lead_id, note.id) for lead_id, note in picked)
    )

    assert [r.status_code for r in responses] == [200] * 4
    decisions = [r.json()["decision"] for r in responses]
    assert sum(d["prompt_sent"] for d in decisions) == 3
    withheld = [d["prompt_withheld"] for d in decisions if not d["prompt_sent"]]
    assert withheld == ["rate_limited"]


async def test_s4_one_note_id_with_two_texts_at_once_is_two_judgements(lane):
    """The same note id sent with two different texts at once on the direct route returns two 200s and no 409."""
    lead_id, note = lane.notes[0]
    edited = f"{note.note} Viewing confirmed for Tuesday at noon."
    lane.script(judgements=2)

    responses = await asyncio.gather(
        lane.judge_direct(lead_id, note, note.note),
        lane.judge_direct(lead_id, note, edited),
    )

    assert [r.status_code for r in responses] == [200, 200]
    assert [r.json()["note_id"] for r in responses] == [note.id, note.id]
