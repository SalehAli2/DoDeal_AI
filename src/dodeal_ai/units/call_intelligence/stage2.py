"""analyse_stage2: wave 2 of one call job, on the stage-2 queue (Unit B).

WHEN. process_call queues it once call.stage1 has gone, for a job eligible for
full analysis (worker.py); the job is `done` and stays done. Its stage2 field
is pending, and this task settles it:

  done    wave 2 ran on the stage-1 transcript (wave2.py); a pass that failed
          leaves its own part null, and the rest stands
  failed  it could not run, and stage2_reason says why:
          stage1_result_gone      the stage-1 result, and its transcript,
                                  expired before stage 2 read it
          llm_not_configured      a worker with no model client
          token_budget_exceeded,  a calls budget spent, or the store that
          audio_budget_exceeded,  counts it down, on the last of the task's
          cost_store_unavailable  runs
          job_deadline_exceeded   cut off by its deadline on the last run

BUDGETS BEFORE SPEND, as process_call: over a calls budget, or with its store
down, nothing is paid for and arq runs the task again after the pause delay
(worker.pause_delay). THE DEADLINE is process_call's too: a run stops itself
before arq cancels it and is run again. Either way the re-run resumes from the
passes kept (paid.py), and only on the task's last run does stage 2 fail.

A job gone, or whose stage 2 is not pending, is left alone: this task may run
more than once for a job, and only the first run finds anything to do.

The arq job carries only (tenant, job_id); everything else is read from db3.
Ids, counts and fixed words on its one outcome line; never a word said.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from arq import Retry

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.cost.limiter import CallsBudgetPaused, calls_budget_preflight
from dodeal_ai.core.cost.spend import spending
from dodeal_ai.core.jobs import (
    Job,
    JobStoreUnavailable,
    Stage2State,
    read_job,
    read_result,
    read_work,
    settle_stage2,
)
from dodeal_ai.core.logging_config import job_log_context
from dodeal_ai.units.call_intelligence.analysis import NO_CLIENT
from dodeal_ai.units.call_intelligence.config import resolve_calls_config
from dodeal_ai.units.call_intelligence.paid import JobGone, PassUsage
from dodeal_ai.units.call_intelligence.transcriber import Transcript
from dodeal_ai.units.call_intelligence.wave2 import Wave2, wave2
from dodeal_ai.units.call_intelligence.worker import (
    DEADLINE,
    deadline_seconds,
    job_scope,
    pause_delay,
)

_logger = logging.getLogger("dodeal_ai.unit_b")

# Why stage 2 could not run: its transcript expired with the stage-1 result.
RESULT_GONE = "stage1_result_gone"

# How long a run that found the job store down, or was cut off, waits before
# arq runs it again.
RETRY_DELAY_SECONDS = 30

# arq's runs of one analyse_stage2: the first, and re-runs after a pause, an
# outage or a deadline; each resumes from the passes kept, never re-paying one.
# On the last, stage 2 fails rather than wait again (workers/calls.py).
STAGE2_TRIES = 5


@dataclass(slots=True)
class Stage2Run:
    """What one run did, for its one outcome line."""

    started: float = field(default_factory=time.monotonic)
    job: Job | None = None
    state: str | None = None
    reason: str | None = None
    wave: Wave2 | None = None
    usage: PassUsage = field(default_factory=PassUsage)


async def analyse_stage2(ctx: dict[str, Any], tenant: str, job_id: str) -> None:
    """The arq task. Every line it logs names the job, and a run that found
    the job ends in one outcome line."""
    run = Stage2Run()
    with (
        job_log_context(tenant=tenant, job_id=job_id) as fields,
        spending("unit_b"),
    ):
        try:
            await _within_deadline(ctx, tenant, job_id, run, fields)
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


async def _within_deadline(
    ctx: dict[str, Any],
    tenant: str,
    job_id: str,
    run: Stage2Run,
    fields: dict[str, str],
) -> None:
    """_analyse under the run's deadline; past it, run again or, on the last
    run, fail."""
    deadline = asyncio.timeout(deadline_seconds())
    try:
        async with deadline:
            await _analyse(ctx, tenant, job_id, run, fields)
    except TimeoutError:
        if not deadline.expired():
            raise
        _logger.warning(
            "call_stage2_deadline_exceeded", extra={"reason_code": DEADLINE}
        )
        await _again_or_fail(ctx, run, DEADLINE, RETRY_DELAY_SECONDS)


async def _analyse(
    ctx: dict[str, Any],
    tenant: str,
    job_id: str,
    run: Stage2Run,
    fields: dict[str, str],
) -> None:
    job = await read_job(tenant, job_id)
    if job is None or job.stage2 is not Stage2State.PENDING:
        return
    fields["request_id"] = job.request_id
    run.job = job
    result = await read_result(tenant, job_id)
    kept = None if result is None else result.get("transcript")
    if not isinstance(kept, dict):
        await _settle(job, run, Stage2State.FAILED, RESULT_GONE)
        return
    client = ctx.get("llm")
    if client is None:
        await _settle(job, run, Stage2State.FAILED, NO_CLIENT)
        return
    scope = job_scope(job)
    try:
        await calls_budget_preflight(scope)
    except CallsBudgetPaused as paused:
        outages = int(ctx.get("job_try", 1))
        window = get_settings().cost_window_seconds
        delay = pause_delay(paused.reason_code, outages, window_seconds=window)
        await _again_or_fail(ctx, run, paused.reason_code, delay)
        return
    config = await resolve_calls_config(tenant)
    try:
        run.wave = await wave2(
            client,
            job,
            config,
            Transcript.model_validate(kept),
            work=await read_work(tenant, job_id),
            scope=scope,
            settings=get_settings(),
            usage=run.usage,
        )
    except JobGone:
        return
    await _settle(job, run, Stage2State.DONE, None)


async def _again_or_fail(
    ctx: dict[str, Any], run: Stage2Run, reason: str, delay: int
) -> None:
    """Run again after `delay`, or on the task's last run fail with `reason`:
    arq runs it no more, and a stage 2 left pending would never settle."""
    if run.job is not None and int(ctx.get("job_try", 1)) >= STAGE2_TRIES:
        await _settle(run.job, run, Stage2State.FAILED, reason)
        return
    run.state, run.reason = Stage2State.PENDING.value, reason
    raise Retry(defer=delay)


async def _settle(
    job: Job, run: Stage2Run, state: Stage2State, reason: str | None
) -> None:
    """Stage 2 out of pending, and the run's record of it."""
    await settle_stage2(job, state, now=datetime.now(UTC), reason=reason)
    run.state, run.reason = state.value, reason


def _log_outcome(run: Stage2Run) -> None:
    """ONE line per run that found its job: what stage 2 became, how long the
    run took, and which parts are null and why."""
    elapsed = time.monotonic() - run.started
    level = logging.WARNING if run.state == Stage2State.FAILED else logging.INFO
    _logger.log(
        level,
        "call_stage2_outcome",
        extra={
            "stage2": run.state,
            "reason": run.reason,
            "elapsed_ms": int(elapsed * 1000),
            "part_reasons": None if run.wave is None else run.wave.reasons or None,
        },
    )
