"""The stuck-job sweep (Unit B): a job left too long is put back on its queue
once, and process_call's own rules decide what it becomes.

WHAT IS STUCK, by the job's score in the active set (core/jobs.py):

  running  downloading, transcribing, analysing or delivering, and not moved
           for CALL_JOB_TIMEOUT_SECONDS + 300. No run lasts longer than the job
           timeout (worker.py stops itself before it), so no run is working on
           it: its worker died, or arq gave up on it.
  queued   not moved for 3600 s, and held by arq under none of its ids.
  paused   300 s past its wake-up, and held by arq under none of its ids.

A queued or paused job arq still holds is only waiting -- behind a backlog, or
on its resume -- and is left alone: a second copy could run beside it and pay
twice. Its mark and its sweep count are taken back.

ONCE PER STALL. The sweep marks a job with the transition it found
(core/jobs.py); the same stall is never re-enqueued twice, and a job that
moves and stalls again can be. The re-run takes the interrupted path: a
transcription or a pass is retried once, a download or a delivery spends an
attempt, and past those the job is dead-lettered and call.failed goes.

A LOST STAGE 2. A done job whose stage 2 has been pending 7200 s, held by arq
under neither of its stage-2 ids, is re-queued ONCE, and waited for 7200 s
more; found so again, its stage 2 fails with stage2_lost and call.failed goes
for stage 2 only -- the job stays done and stage 1 stands. The re-run resumes
from the passes kept (paid.py), so nothing received is paid for twice. A stage
2 arq still holds is only waiting (a pause over budget waits a whole window)
and is left alone.

It runs every 300 s on the normal queue's worker only (workers/calls.py).
Ids and counts on its lines; never a link, a hash or a word said.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dodeal_ai.core.callbacks import CALL_FAILED
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.jobs import (
    JobStatus,
    Stage2State,
    mark_stage2_requeued,
    mark_swept,
    read_job,
    settle_stage2,
    stage2_stuck,
    stale_jobs,
    stale_stage2,
    unmark_swept,
)
from dodeal_ai.units.call_intelligence.config import resolve_calls_config
from dodeal_ai.units.call_intelligence.queues import (
    enqueue_call,
    enqueue_stage2,
    on_the_queue,
    stage2_on_the_queue,
)
from dodeal_ai.units.call_intelligence.worker import (
    Deliver,
    deliver_nothing,
    owed_delivery,
)

_logger = logging.getLogger("dodeal_ai.unit_b")

# How often the sweep runs, and how far past the job timeout a job's last
# transition must be before it counts as stuck.
SWEEP_INTERVAL_SECONDS = 300
SWEEP_GRACE_SECONDS = 300

# How long a job may sit queued unmoved, and a paused one past its wake-up,
# before the sweep asks arq whether it still holds the job.
QUEUED_WAIT_SECONDS = 3600
PAUSED_WAIT_SECONDS = 300

# How long a done job's stage 2 may stay pending before the sweep asks arq
# whether it still holds it: past any stage-2 run and its short retries.
STAGE2_WAIT_SECONDS = 7200
# Why stage 2 failed: lost, re-queued once, and lost again.
STAGE2_LOST = "stage2_lost"

# Stale jobs read per sweep; the rest wait for the next one, oldest first.
SWEEP_BATCH = 500

# The statuses a run holds a job in; a job left in one has lost its run.
STUCK_STATUSES = frozenset(
    {
        JobStatus.DOWNLOADING,
        JobStatus.TRANSCRIBING,
        JobStatus.ANALYSING,
        JobStatus.DELIVERING,
    }
)

# The statuses a job waits in on purpose, while arq holds it.
WAITING_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.PAUSED_BUDGET})


def stuck_after() -> dict[JobStatus, int]:
    """Seconds past its score a job in each swept status is stuck."""
    running = get_settings().call_job_timeout_seconds + SWEEP_GRACE_SECONDS
    return {
        **dict.fromkeys(STUCK_STATUSES, running),
        JobStatus.QUEUED: QUEUED_WAIT_SECONDS,
        JobStatus.PAUSED_BUDGET: PAUSED_WAIT_SECONDS,
    }


async def sweep_stuck_jobs(ctx: dict[str, Any]) -> int:
    """The arq cron task: re-enqueue every stuck job once. The count swept."""
    deliver: Deliver = ctx.get("deliver", deliver_nothing)
    return await sweep(now=datetime.now(UTC), deliver=deliver)


async def sweep(*, now: datetime, deliver: Deliver = deliver_nothing) -> int:
    """One pass over the stale end of the active set, then of the stage-2
    pending set. A queue that cannot be reached ends the pass, anything it
    had marked taken back, for the next one."""
    swept = 0
    waits = stuck_after()
    before = now - timedelta(seconds=min(waits.values()))
    try:
        for tenant, job_id in await stale_jobs(before, limit=SWEEP_BATCH):
            swept += await _sweep_job(tenant, job_id, now, waits)
        lost = now - timedelta(seconds=STAGE2_WAIT_SECONDS)
        for tenant, job_id in await stale_stage2(lost, limit=SWEEP_BATCH):
            swept += await _sweep_stage2(tenant, job_id, now, deliver)
    except QueueUnavailable:
        _logger.warning(
            "call_job_sweep_stopped",
            extra={"reason_code": "queue_unavailable", "swept": swept},
        )
    return swept


async def _sweep_job(
    tenant: str, job_id: str, now: datetime, waits: dict[JobStatus, int]
) -> int:
    """Re-enqueue one stale job if it is stuck: 1 when it was, else 0."""
    marked = await mark_swept(tenant, job_id, now=now, waits=waits)
    if marked is None:
        return 0
    try:
        if marked.status in WAITING_STATUSES and await on_the_queue(
            tenant, job_id, pauses=marked.pauses, sweeps=marked.sweeps - 1
        ):
            await unmark_swept(tenant, job_id, uncount=True)
            return 0
        await enqueue_call(tenant, job_id, marked.queue, sweep=marked.sweeps)
    except QueueUnavailable:
        await unmark_swept(tenant, job_id)
        raise
    _logger.warning(
        "call_job_swept",
        extra={
            "tenant": tenant,
            "job_id": job_id,
            "sweeps": marked.sweeps,
            "status": marked.status.value,
        },
    )
    return 1


async def _sweep_stage2(
    tenant: str, job_id: str, now: datetime, deliver: Deliver
) -> int:
    """Re-queue a lost stage 2 once, or fail it once re-queued: 1 when it was
    lost, else 0."""
    requeued = await stage2_stuck(
        tenant, job_id, now=now, wait_seconds=STAGE2_WAIT_SECONDS
    )
    if requeued is None or await stage2_on_the_queue(tenant, job_id):
        return 0
    if requeued == 0:
        await enqueue_stage2(tenant, job_id, sweep=True)
        await mark_stage2_requeued(tenant, job_id, now=now)
        _logger.warning("call_stage2_swept", extra={"tenant": tenant, "job_id": job_id})
        return 1
    job = await read_job(tenant, job_id)
    config = await resolve_calls_config(tenant)
    if job is None or not await settle_stage2(
        job,
        Stage2State.FAILED,
        now=now,
        reason=STAGE2_LOST,
        delivery=owed_delivery(config),
    ):
        return 0
    _logger.warning(
        "call_stage2_lost",
        extra={"tenant": tenant, "job_id": job_id, "reason_code": STAGE2_LOST},
    )
    await deliver(job, CALL_FAILED, config)
    return 1
