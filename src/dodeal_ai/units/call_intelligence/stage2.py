"""analyse_stage2: wave 2 of one call job, on the stage-2 queue (Unit B).

WHEN. process_call queues it once call.stage1 has gone, for a job eligible for
full analysis (worker.py); the job is `done` and stays done. Its stage2 field
is pending, and this task settles it:

  done    wave 2 ran on the stage-1 transcript
  failed  it could not run, and stage2_reason says why:
          stage1_result_gone  the stage-1 result, and its transcript, expired
                              before stage 2 read it

A job gone, or whose stage 2 is not pending, is left alone: this task may run
more than once for a job, and only the first run finds anything to do.

The arq job carries only (tenant, job_id); everything else is read from db3.
Ids, counts and fixed words on its one outcome line; never a word said.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from arq import Retry

from dodeal_ai.core.jobs import (
    Job,
    JobStoreUnavailable,
    Stage2State,
    read_job,
    read_result,
    settle_stage2,
)
from dodeal_ai.core.logging_config import job_log_context

_logger = logging.getLogger("dodeal_ai.unit_b")

# Why stage 2 could not run: its transcript expired with the stage-1 result.
RESULT_GONE = "stage1_result_gone"

# How long a run that found the job store down waits before arq runs it again.
RETRY_DELAY_SECONDS = 30


@dataclass(slots=True)
class Stage2Run:
    """What one run did, for its one outcome line."""

    started: float = field(default_factory=time.monotonic)
    job: Job | None = None
    state: str | None = None
    reason: str | None = None


async def analyse_stage2(ctx: dict[str, Any], tenant: str, job_id: str) -> None:
    """The arq task. Every line it logs names the job, and a run that found
    the job ends in one outcome line."""
    run = Stage2Run()
    with job_log_context(tenant=tenant, job_id=job_id) as fields:
        try:
            await _analyse(tenant, job_id, run, fields)
        except JobStoreUnavailable:
            # Nothing can be read or recorded; arq runs the task again shortly.
            _logger.warning(
                "call_stage2_store_unavailable",
                extra={"reason_code": "job_store_unavailable"},
            )
            raise Retry(defer=RETRY_DELAY_SECONDS) from None
        finally:
            if run.job is not None:
                _log_outcome(run)


async def _analyse(
    tenant: str, job_id: str, run: Stage2Run, fields: dict[str, str]
) -> None:
    job = await read_job(tenant, job_id)
    if job is None or job.stage2 is not Stage2State.PENDING:
        return
    fields["request_id"] = job.request_id
    run.job = job
    result = await read_result(tenant, job_id)
    if result is None or not isinstance(result.get("transcript"), dict):
        await _settle(job, run, Stage2State.FAILED, RESULT_GONE)
        return
    await _settle(job, run, Stage2State.DONE, None)


async def _settle(
    job: Job, run: Stage2Run, state: Stage2State, reason: str | None
) -> None:
    """Stage 2 out of pending, and the run's record of it."""
    await settle_stage2(job, state, now=datetime.now(UTC), reason=reason)
    run.state, run.reason = state.value, reason


def _log_outcome(run: Stage2Run) -> None:
    """ONE line per run that found its job: what stage 2 became, and how long
    the run took."""
    elapsed = time.monotonic() - run.started
    level = logging.WARNING if run.state == Stage2State.FAILED else logging.INFO
    _logger.log(
        level,
        "call_stage2_outcome",
        extra={
            "stage2": run.state,
            "reason": run.reason,
            "elapsed_ms": int(elapsed * 1000),
        },
    )
