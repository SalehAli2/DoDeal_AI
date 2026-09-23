"""The stuck-job sweep (Unit B): a job no run has moved for too long is put
back on its queue once, and process_call's own rules decide what it becomes.

WHAT IS STUCK: a job in downloading, transcribing, analysing or delivering
whose last transition is older than CALL_JOB_TIMEOUT_SECONDS + 300. No run
lasts longer than the job timeout (worker.py stops itself before it), so a job
that has not moved for that long plus a margin has no run working on it: its
worker died, or arq gave up on it. Queued and paused jobs are waiting on
purpose and are not swept.

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
from dodeal_ai.units.call_intelligence.queues import enqueue_call

_logger = logging.getLogger("dodeal_ai.unit_b")

# How often the sweep runs, and how far past the job timeout a job's last
# transition must be before it counts as stuck.
SWEEP_INTERVAL_SECONDS = 300
SWEEP_GRACE_SECONDS = 300

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


def stuck_before(now: datetime) -> datetime:
    """A running job whose last transition is before this has no run."""
    wait = get_settings().call_job_timeout_seconds + SWEEP_GRACE_SECONDS
    return now - timedelta(seconds=wait)


async def sweep_stuck_jobs(ctx: dict[str, Any]) -> int:
    """The arq cron task: re-enqueue every stuck job once. The count swept."""
    return await sweep(now=datetime.now(UTC))


async def sweep(*, now: datetime) -> int:
    """One pass over the stale end of the active set. A queue that cannot be
    reached ends the pass, the job's mark taken back, for the next one."""
    swept = 0
    for tenant, job_id in await stale_jobs(stuck_before(now), limit=SWEEP_BATCH):
        marked = await mark_swept(tenant, job_id, statuses=STUCK_STATUSES)
        if marked is None:
            continue
        sweeps, queue = marked
        try:
            await enqueue_call(tenant, job_id, queue, sweep=sweeps)
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
            extra={"tenant": tenant, "job_id": job_id, "sweeps": sweeps},
        )
    return swept
