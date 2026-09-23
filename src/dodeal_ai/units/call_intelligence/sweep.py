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

It runs every 300 s on the normal queue's worker only (workers/calls.py).
Ids and counts on its lines; never a link, a hash or a word said.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.jobs import JobStatus, mark_swept, stale_jobs, unmark_swept
from dodeal_ai.units.call_intelligence.queues import enqueue_call, on_the_queue

_logger = logging.getLogger("dodeal_ai.unit_b")

# How often the sweep runs, and how far past the job timeout a job's last
# transition must be before it counts as stuck.
SWEEP_INTERVAL_SECONDS = 300
SWEEP_GRACE_SECONDS = 300

# How long a job may sit queued unmoved, and a paused one past its wake-up,
# before the sweep asks arq whether it still holds the job.
QUEUED_WAIT_SECONDS = 3600
PAUSED_WAIT_SECONDS = 300

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
    return await sweep(now=datetime.now(UTC))


async def sweep(*, now: datetime) -> int:
    """One pass over the stale end of the active set. A queue that cannot be
    reached ends the pass, the job's mark taken back, for the next one."""
    swept = 0
    waits = stuck_after()
    before = now - timedelta(seconds=min(waits.values()))
    for tenant, job_id in await stale_jobs(before, limit=SWEEP_BATCH):
        marked = await mark_swept(tenant, job_id, now=now, waits=waits)
        if marked is None:
            continue
        try:
            if marked.status in WAITING_STATUSES and await on_the_queue(
                tenant, job_id, pauses=marked.pauses, sweeps=marked.sweeps - 1
            ):
                await unmark_swept(tenant, job_id, uncount=True)
                continue
            await enqueue_call(tenant, job_id, marked.queue, sweep=marked.sweeps)
        except QueueUnavailable:
            await unmark_swept(tenant, job_id)
            _logger.warning(
                "call_job_sweep_stopped",
                extra={"reason_code": "queue_unavailable", "swept": swept},
            )
            return swept
        swept += 1
        _logger.warning(
            "call_job_swept",
            extra={
                "tenant": tenant,
                "job_id": job_id,
                "sweeps": marked.sweeps,
                "status": marked.status.value,
            },
        )
    return swept
