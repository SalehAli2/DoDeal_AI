"""process_call: one call job, from the queue to stage 1 (register items 35, 36).

THE ORDER, and what each step may cost:

  1. The job, and the tenant's rules as they are NOW. A job that is gone or
     terminal is left alone; a tenant that switched calls off since the push
     fails it (calls_not_enabled) before anything is spent.
  2. At CALL_MAX_TRIES attempts the job is dead-lettered with its last reason.
  3. FREE OUTCOMES FIRST (item 36): voicemail, no answer, or shorter than
     min_transcribe_seconds is `done` with that outcome label and no paid call.
  4. A STORED TRANSCRIPT IS NEVER RE-PAID: a run that finds one delivers it.
     A run that finds the job `transcribing` with nothing stored may already
     have paid for a transcription that was lost, so it dead-letters
     (transcription_interrupted) rather than pay again (CLAUDE.md: never retry
     a paid call).
  5. The call budgets (core(105)): over one, or with the store down, the job
     PAUSES and is re-queued after the window. Nothing is spent blind.
  6. One attempt is claimed, the recording downloaded (core/audio_download.py)
     and its seconds charged, then transcribed ONCE: a transcription failure is
     never retried. A download that may succeed later is retried until the
     attempts run out.
  7. The transcript is stored for result_ttl_seconds, stage 1 is delivered,
     and the job is `done`. eligible_for_full_analysis is at least
     scoring_min_seconds long and not uncertain.

The arq job carries only (tenant, job_id); everything else is read from db3.
Nothing here logs the link, a hash, a voiceprint or a word of the transcript.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from arq import Retry

from dodeal_ai.core.audio_download import (
    AudioDownloadError,
    downloaded_audio,
    resolve_host,
)
from dodeal_ai.core.callbacks import CALL_FAILED, CALL_STAGE1
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext, TenantScope
from dodeal_ai.core.cost.limiter import (
    CallsBudgetPaused,
    calls_budget_preflight,
    charge_audio_seconds,
)
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.jobs import (
    Job,
    JobStatus,
    JobStoreUnavailable,
    claim_attempt,
    pause,
    read_job,
    read_result,
    store_result,
    transition,
)
from dodeal_ai.units.call_intelligence.config import CallsConfig, resolve_calls_config
from dodeal_ai.units.call_intelligence.queues import enqueue_call
from dodeal_ai.units.call_intelligence.transcriber import (
    Transcriber,
    Transcript,
    TranscriptionError,
)

_logger = logging.getLogger("dodeal_ai.unit_b")

# The stage-1 event, and the one a dead-lettered or failed job sends.
STAGE1 = CALL_STAGE1
FAILED = CALL_FAILED

# How long a retryable failure waits per attempt already made: 30 s, then 60 s.
RETRY_DELAY_SECONDS = 30

# The outcome labels a call can be done with before any paid call.
_UNPAID_OUTCOMES = frozenset({"voicemail", "no_answer"})
TOO_SHORT = "too_short"

# Delivers one event for a job; True once the CRM has it (or there is no
# callback to send), False when delivery is left to its retry schedule.
type Deliver = Callable[[Job, str, CallsConfig], Awaitable[bool]]


async def deliver_nothing(job: Job, event: str, config: CallsConfig) -> bool:
    """The deliverer before callbacks exist: nothing to send, nothing pending."""
    return True


def _now() -> datetime:
    return datetime.now(UTC)


def job_scope(job: Job) -> TenantScope:
    """The job's scope: the tenant its push was admitted for, the author the
    CRM named, and the calls budget."""
    author_id = int(str(job.metadata["author_id"]))
    return RequestContext.for_admitted_job(
        job.tenant, request_id=job.request_id
    ).scope_for_author(author_id, budget="calls")


def unpaid_outcome(job: Job, config: CallsConfig) -> str | None:
    """The outcome label a call is done with unpaid, or None to transcribe."""
    outcome = job.metadata.get("call_outcome")
    if isinstance(outcome, str) and outcome in _UNPAID_OUTCOMES:
        return outcome
    if int(str(job.metadata["duration_seconds"])) < config.min_transcribe_seconds:
        return TOO_SHORT
    return None


def stage1_result(
    job: Job,
    config: CallsConfig,
    *,
    transcript: Transcript | None,
    outcome_label: str | None,
) -> dict[str, object]:
    """The stage-1 result, held in db3 and delivered as call.stage1."""
    duration = int(str(job.metadata["duration_seconds"]))
    eligible = (
        transcript is not None
        and not transcript.uncertain
        and duration >= config.scoring_min_seconds
    )
    return {
        "stage": 1,
        "call_id": job.call_id,
        "duration_seconds": duration,
        "outcome_label": outcome_label,
        "eligible_for_full_analysis": eligible,
        "transcript": None
        if transcript is None
        else transcript.model_dump(mode="json"),
    }


async def process_call(ctx: dict[str, Any], tenant: str, job_id: str) -> None:
    """The arq task. See the module docstring for the order."""
    try:
        await _process(ctx, tenant, job_id)
    except JobStoreUnavailable:
        # Nothing can be recorded; arq runs the job again shortly.
        _logger.warning(
            "call_job_store_unavailable",
            extra={"reason_code": "job_store_unavailable", "tenant": tenant},
        )
        raise Retry(defer=RETRY_DELAY_SECONDS) from None


async def _process(ctx: dict[str, Any], tenant: str, job_id: str) -> None:
    settings = get_settings()
    job = await read_job(tenant, job_id)
    if job is None or job.terminal:
        return
    config = await resolve_calls_config(tenant)
    if not config.calls_enabled:
        await _fail(ctx, job, config, "calls_not_enabled", dead=False)
        return
    if job.attempts >= settings.call_max_tries:
        await _fail(ctx, job, config, job.reason or "max_tries_exceeded", dead=True)
        return

    label = unpaid_outcome(job, config)
    stored = None if label is not None else await read_result(tenant, job_id)
    paid = label is None and stored is None
    if paid and job.status is JobStatus.TRANSCRIBING:
        await _fail(ctx, job, config, "transcription_interrupted", dead=True)
        return

    scope = job_scope(job)
    if paid:
        try:
            await calls_budget_preflight(scope)
        except CallsBudgetPaused as paused:
            await _pause(job, paused.reason_code)
            return

    attempt = await claim_attempt(
        job,
        max_tries=settings.call_max_tries,
        now=_now(),
        status=JobStatus.DOWNLOADING if paid else JobStatus.DELIVERING,
    )
    if attempt is None:
        return
    if attempt == 0:
        await _fail(ctx, job, config, job.reason or "max_tries_exceeded", dead=True)
        return

    if label is not None:
        result = stage1_result(job, config, transcript=None, outcome_label=label)
        await store_result(
            tenant, job_id, result, ttl_seconds=config.result_ttl_seconds
        )
    elif paid:
        transcript = await _transcribe(ctx, job, config, scope, attempt)
        if transcript is None:
            return
        result = stage1_result(job, config, transcript=transcript, outcome_label=None)
        await store_result(
            tenant, job_id, result, ttl_seconds=config.result_ttl_seconds
        )

    await transition(
        job, JobStatus.DELIVERING, now=_now(), ttl_seconds=config.result_ttl_seconds
    )
    deliver: Deliver = ctx.get("deliver", deliver_nothing)
    if await deliver(job, STAGE1, config):
        await transition(
            job,
            JobStatus.DONE,
            now=_now(),
            reason=label,
            ttl_seconds=config.result_ttl_seconds,
        )
        _logger.info(
            "call_job_done",
            extra={"tenant": tenant, "job_id": job_id, "outcome_label": label},
        )


async def _transcribe(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    scope: TenantScope,
    attempt: int,
) -> Transcript | None:
    """Download, charge the seconds, transcribe once. None when the job was
    failed, paused or scheduled to retry instead."""
    settings = get_settings()
    transcriber: Transcriber = ctx["transcriber"]
    meta = job.metadata
    try:
        async with downloaded_audio(
            str(meta["audio_url"]),
            expires_at=datetime.fromisoformat(str(meta["audio_url_expires_at"])),
            now=_now(),
            allowed_hosts=config.audio_hosts,
            max_bytes=config.max_audio_bytes,
            timeout_seconds=settings.call_download_timeout_seconds,
            http=ctx["http"],
            resolve=ctx.get("resolve", resolve_host),
        ) as audio:
            try:
                await charge_audio_seconds(scope, int(str(meta["duration_seconds"])))
            except CallsBudgetPaused as paused:
                await _pause(job, paused.reason_code)
                return None
            await transition(
                job,
                JobStatus.TRANSCRIBING,
                now=_now(),
                ttl_seconds=config.result_ttl_seconds,
            )
            hint = meta.get("language_hint")
            return await transcriber.transcribe(
                audio.path, language_hint=hint if isinstance(hint, str) else None
            )
    except AudioDownloadError as refused:
        await _download_failed(ctx, job, config, refused, attempt)
        return None
    except TranscriptionError as failed:
        # Never retried, retryable or not: the provider may have billed it.
        await _fail(ctx, job, config, failed.reason, dead=False)
        return None


async def _download_failed(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    refused: AudioDownloadError,
    attempt: int,
) -> None:
    """A refused link fails the job; one that may work later is retried until
    the attempts run out, then dead-lettered with its reason."""
    if not refused.retryable:
        await _fail(ctx, job, config, refused.reason, dead=False)
        return
    if attempt >= get_settings().call_max_tries:
        await _fail(ctx, job, config, refused.reason, dead=True)
        return
    await transition(
        job,
        JobStatus.QUEUED,
        now=_now(),
        reason=refused.reason,
        ttl_seconds=config.result_ttl_seconds,
    )
    raise Retry(defer=RETRY_DELAY_SECONDS * attempt)


async def _fail(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    reason: str,
    *,
    dead: bool,
) -> None:
    """failed or dead_letter with `reason`, then call.failed to the CRM."""
    status = JobStatus.DEAD_LETTER if dead else JobStatus.FAILED
    moved = await transition(
        job, status, now=_now(), reason=reason, ttl_seconds=config.result_ttl_seconds
    )
    if not moved:
        return
    _logger.warning(
        "call_job_failed",
        extra={
            "reason_code": reason,
            "tenant": job.tenant,
            "job_id": job.job_id,
            "status": status.value,
        },
    )
    deliver: Deliver = ctx.get("deliver", deliver_nothing)
    await deliver(job, FAILED, config)


async def _pause(job: Job, reason: str) -> None:
    """paused_budget, and back on the job's queue after the cost window."""
    settings = get_settings()
    count = await pause(job, now=_now(), reason=reason)
    if count is None:
        return
    _logger.warning(
        "call_job_paused",
        extra={"reason_code": reason, "tenant": job.tenant, "job_id": job.job_id},
    )
    try:
        await enqueue_call(
            job.tenant,
            job.job_id,
            job.queue,
            resume=count,
            defer=timedelta(seconds=settings.cost_window_seconds),
        )
    except QueueUnavailable:
        # Paused with nothing to wake it: run this again soon, which pauses
        # again and queues the resume then.
        raise Retry(defer=RETRY_DELAY_SECONDS) from None
