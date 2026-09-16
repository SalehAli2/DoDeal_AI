"""The load lane's smoke test: one judgement through the served app on real Redis."""

from __future__ import annotations


async def test_one_judgement_is_200_and_counts_once_on_the_real_cost_store(lane):
    """One judgement returns 200 and moves the per-user request counter to exactly 1."""
    lead_id, note = lane.notes[0]
    lane.script(judgements=1)

    response = await lane.judge(lead_id, note.id)

    assert response.status_code == 200
    assert response.json()["note_id"] == note.id
    assert await lane.stores.cost.get(lane.user_cost_key) == "1"
    assert lane.llm.call_count == 3
