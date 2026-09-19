"""Load scenario 7b: a burst on a small pool, under Redis latency, with cancellations.

Sixty-four judgements on distinct notes share a pool of 8 connections per store
while a client of the test's own slows the server in pulses. Every classify call
waits on one event until all 64 have reached it, so the seeded third cancelled
then each hold a live reservation when the cancellation lands.

LATENCY. `DEBUG SLEEP` blocks the whole server, but Redis 7 refuses DEBUG unless
`enable-debug-command` allows the caller; the pulses are then `CLIENT PAUSE <ms>
WRITE`, which holds every write and every script. The method that ran is
recorded on the test as the user property `latency_method`.
"""

from __future__ import annotations

import asyncio
import random

import httpx
import pytest
import redis
from redis import asyncio as redis_async

from dodeal_ai.core.breaker import BreakerState, operational_breaker
from dodeal_ai.core.config import get_settings
from dodeal_ai.units.structured_intelligence.schemas import Judgement
from dodeal_ai.units.structured_intelligence.state import _RESERVED
from tests.helpers.fake_llm import FakeLLM

BURST = 64
POOL = 8
DEADLINE_SECONDS = 2.0
# Picks the cancelled third. Fixed, so a red run cancels the same tasks again.
CANCEL_SEED = 7402
CANCELLED = BURST // 3
# One pulse slows the server for PULSE_MS, then leaves it alone for GAP_SECONDS:
# a stall every ~150 ms. At a 25 ms gap the pool of 8 starves into socket
# timeouts and no longer measures latency but an outage, which scenario 6 owns.
PULSE_MS = 50
GAP_SECONDS = 0.1
# How often the test looks at the classify count while the burst reaches it.
POLL_SECONDS = 0.005
# A confirmed key's floor: far above the 8 s reservation, far below the day it gets.
CONFIRMED_TTL_FLOOR = 3600


@pytest.fixture(autouse=True)
def small_pool_short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scenario's settings, set before `lane` clears the settings cache."""
    monkeypatch.setenv("DODEAL_REDIS_MAX_CONNECTIONS", str(POOL))
    monkeypatch.setenv("DODEAL_JUDGEMENT_DEADLINE_SECONDS", str(DEADLINE_SECONDS))
    # The in-flight cap at the burst: the default 32 would shed half of it at the
    # door, and a shed request never reaches the pool or the classify call.
    monkeypatch.setenv("DODEAL_MAX_INFLIGHT", str(BURST))


async def _pulse(client: redis_async.Redis, stop: asyncio.Event) -> tuple[str, int]:
    """Slow the server in pulses until `stop` is set; the method that ran, and how
    many pulses it sent."""
    try:
        await client.execute_command("DEBUG", "SLEEP", "0")
    except redis.ResponseError:
        method = "client_pause"
    else:
        method = "debug_sleep"
    pulses = 0
    try:
        while not stop.is_set():
            if method == "debug_sleep":
                # Blocks the server, and so this call, for the whole pulse.
                await client.execute_command("DEBUG", "SLEEP", PULSE_MS / 1000)
            else:
                # Returns at once; the pause runs on without this call.
                await client.execute_command("CLIENT", "PAUSE", PULSE_MS, "WRITE")
                await asyncio.sleep(PULSE_MS / 1000)
            pulses += 1
            await asyncio.sleep(GAP_SECONDS)
    finally:
        if method == "client_pause":
            await client.execute_command("CLIENT", "UNPAUSE")
    return method, pulses


async def _all_in_classify(llm: FakeLLM, count: int) -> None:
    """Return once `count` calls wait inside the fake, or fail at the deadline."""
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            while llm.call_count < count:
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        pytest.fail(f"{llm.call_count} of {count} judgements reached classify in time")


async def test_s7b_a_burst_on_a_small_pool_under_latency_with_cancellations_leaks_no_reservation(
    lane, every_usable_note, request
):
    """Sixty-four judgements on a pool of 8 under latency pulses, a seeded third cancelled mid-classify, leave the breaker closed, no reservation and every finished judgement confirmed long."""
    settings = get_settings()
    assert (settings.redis_pool_size, settings.judgement_deadline_seconds) == (
        POOL,
        DEADLINE_SECONDS,
    )
    picked = every_usable_note[:BURST]
    assert len({note.id for _, note in picked}) == BURST
    lane.script(judgements=BURST)
    # Every call waits on `released`: classify parks until it is set, and the two
    # passes after classify only start once it has been, so they run straight on.
    lane.llm.hold_after = 0
    doomed = set(random.Random(CANCEL_SEED).sample(range(BURST), CANCELLED))

    pulser_client = redis_async.from_url(lane.stores.cost_url, decode_responses=True)
    stop = asyncio.Event()
    pulser = asyncio.create_task(_pulse(pulser_client, stop))
    tasks = [
        asyncio.create_task(lane.judge(lead_id, note.id)) for lead_id, note in picked
    ]
    try:
        await _all_in_classify(lane.llm, BURST)
        for index in doomed:
            tasks[index].cancel()
        await asyncio.gather(*(tasks[i] for i in doomed), return_exceptions=True)
        lane.llm.released.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # Nothing outlives the test: no parked call, no pulse, no running request.
        lane.llm.released.set()
        stop.set()
        method, pulses = await pulser
        await pulser_client.aclose()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    request.node.user_properties.append(("latency_method", method))

    assert pulses > 0
    assert all(tasks[i].cancelled() for i in doomed)
    survivors = [o for i, o in enumerate(outcomes) if i not in doomed]
    assert [
        o.status_code if isinstance(o, httpx.Response) else type(o).__name__
        for o in survivors
    ] == [200] * (BURST - CANCELLED)

    operational = lane.stores.operational
    held = {}
    async for key in operational.scan_iter(match="idem:*"):
        held[key] = (await operational.get(key), await operational.ttl(key))

    assert operational_breaker().state is BreakerState.CLOSED
    assert sum(value == _RESERVED for value, _ in held.values()) == 0
    # Register items 1 and 2: every confirmed key holds a judgement that validates.
    assert [
        (Judgement.model_validate_json(value).note_id > 0, ttl > CONFIRMED_TTL_FLOOR)
        for value, ttl in held.values()
    ] == [(True, True)] * (BURST - CANCELLED)
