"""Load scenario 6: a burst while the operational store is unreachable.

The reservation fails closed, so every request must be refused with 503
idempotency_unavailable, and quickly: a dead db2 must cost a connect budget, not
a hung request.
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

BURST = 10
# The ceiling on any one request, far above the 0.25 s connect budget and well
# below the judgement deadline, so a request that waited on the store fails here.
MAX_SECONDS = 2.0


@pytest.fixture
def operational_url() -> str:
    """A db 11 URL on a loopback port nothing listens on, found by binding a
    socket and closing it again."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"redis://127.0.0.1:{port}/11"


async def test_s6_a_burst_on_a_dead_operational_store_is_all_fast_503s(lane):
    """Ten requests at once with db 11 unreachable all return 503 idempotency_unavailable, none taking over 2.0 s."""
    picked = lane.notes[:BURST]

    async def timed(lead_id: int, note_id: int):
        started = time.monotonic()
        response = await lane.judge(lead_id, note_id)
        return response, time.monotonic() - started

    results = await asyncio.gather(
        *(timed(lead_id, note.id) for lead_id, note in picked)
    )

    assert [r.status_code for r, _ in results] == [503] * BURST
    assert {r.json()["reason"] for r, _ in results} == {"idempotency_unavailable"}
    assert max(seconds for _, seconds in results) <= MAX_SECONDS
    assert lane.llm.call_count == 0
