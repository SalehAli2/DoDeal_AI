"""process_call: one call job, from the queue to stage 1 (register items 35, 36).

THE ORDER, and what each step may cost:

  1. The job, and the tenant's rules as they are NOW. A job that is gone or
     terminal is left alone; a tenant that switched calls off since the push
     fails it (calls_not_enabled) before anything is spent.
  2. At CALL_MAX_TRIES attempts the job is dead-lettered with its last reason.
  3. FREE OUTCOMES FIRST (item 36): voicemail, no answer, or shorter than
     min_transcribe_seconds is `done` with that outcome label and no paid call.
  4. A STORED TRANSCRIPT IS NEVER RE-PAID: a run that finds one delivers it.
     A run that finds the job `transcribing` with nothing stored was
     interrupted: it is transcribed again ONCE (BRD B6, the lead's override of
     CLAUDE.md's never-retry rule for this case only), and the second
     interruption dead-letters (transcription_interrupted).
  5. The call budgets (core(105)): over one, or with the store down, the job
     PAUSES and is re-queued after the window. Nothing is spent blind.
  6. One attempt is claimed, the recording downloaded (core/audio_download.py)
     and its seconds charged, then transcribed. A transcription that failed
     and may succeed later is retried ONCE, as an attempt, its seconds charged
     again; the second failure is dead_letter and call.failed. A download that
     may succeed later is retried until the attempts run out.
  7. The transcript is stored for result_ttl_seconds, stage 1 is delivered,
     and the job is `done`. eligible_for_full_analysis is at least
     scoring_min_seconds long and not uncertain.

The arq job carries only (tenant, job_id); everything else is read from db3.
Nothing here logs the link, a hash, a voiceprint or a word of the transcript.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from arq import Retry

from dodeal_ai.core import metrics
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
from dodeal_ai.core.cost.spend import current_spend, spending
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.jobs import (
    Job,
    JobStatus,
    JobStoreUnavailable,
    claim_attempt,
    pause,
    read_job,
    read_result,
    start_transcription,
    store_result,
    transition,
)
from dodeal_ai.core.logging_config import job_log_context
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

# Transcriptions a job may start, each paid: the first and ONE retry (item 35,
# BRD B6). Fixed by the brief, not a setting: a third is never paid for.
TRANSCRIPTION_TRIES = 2
INTERRUPTED = "transcription_interrupted"

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


@dataclass(slots=True)
class CallRun:
    """What one task run did, for its one outcome line (register item 54).
    A stage that did not run stays None: it did not take zero milliseconds."""

    started: float = field(default_factory=time.monotonic)
    job: Job | None = None
    status: str | None = None
    reason: str | None = None
    attempts: int | None = None
    bytes: int | None = None
    transcript: Transcript | None = None
    download_ms: int | None = None
    transcribe_ms: int | None = None
    analyse_ms: int | None = None
    deliver_ms: int | None = None

    def moved(self, status: JobStatus, reason: str | None = None) -> None:
        self.status, self.reason = status.value, reason


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def process_call(ctx: dict[str, Any], tenant: str, job_id: str) -> None:
    """The arq task. See the module docstring for the order. Every line it
    logs names the job (core/logging_config.py), and it ends in one outcome
    line whatever happened."""
    run = CallRun()
    with (
        job_log_context(tenant=tenant, job_id=job_id) as fields,
        spending("unit_b"),
    ):
        try:
            await _process(ctx, tenant, job_id, run, fields)
        except JobStoreUnavailable:
            # Nothing can be recorded; arq runs the job again shortly.
            _logger.warning(
                "call_job_store_unavailable",
                extra={"reason_code": "job_store_unavailable"},
            )
            raise Retry(defer=RETRY_DELAY_SECONDS) from None
        except Retry:
            raise
        except BaseException:
            run.status = "error"
            raise
        finally:
            if run.job is not None:
                _log_outcome(run)


async def _process(
    ctx: dict[str, Any],
    tenant: str,
    job_id: str,
    run: CallRun,
    fields: dict[str, str],
) -> None:
    settings = get_settings()
    job = await read_job(tenant, job_id)
    if job is None:
        return
    fields["request_id"] = job.request_id
    run.job, run.status, run.reason, run.attempts = (
        job,
        job.status.value,
        job.reason,
        job.attempts,
    )
    if job.terminal:
        return
    config = await resolve_calls_config(tenant)
    if not config.calls_enabled:
        await _fail(ctx, job, config, "calls_not_enabled", dead=False, run=run)
        return
    label = unpaid_outcome(job, config)
    stored = None if label is not None else await read_result(tenant, job_id)
    paid = label is None and stored is None
    # `transcribing` with nothing stored: the last run died mid-transcription.
    interrupted = paid and job.status is JobStatus.TRANSCRIBING
    if job.attempts >= settings.call_max_tries or (
        interrupted and job.transcriptions >= TRANSCRIPTION_TRIES
    ):
        reason = INTERRUPTED if interrupted else job.reason or "max_tries_exceeded"
        await _fail(ctx, job, config, reason, dead=True, run=run)
        return

    scope = job_scope(job)
    if paid:
        try:
            await calls_budget_preflight(scope)
        except CallsBudgetPaused as paused:
            await _pause(job, paused.reason_code, run=run)
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
        reason = job.reason or "max_tries_exceeded"
        await _fail(ctx, job, config, reason, dead=True, run=run)
        return
    run.attempts = attempt

    if label is not None:
        result = stage1_result(job, config, transcript=None, outcome_label=label)
        await store_result(
            tenant, job_id, result, ttl_seconds=config.result_ttl_seconds
        )
    elif paid:
        transcript = await _transcribe(ctx, job, config, scope, attempt, run)
        if transcript is None:
            return
        result = stage1_result(job, config, transcript=transcript, outcome_label=None)
        await store_result(
            tenant, job_id, result, ttl_seconds=config.result_ttl_seconds
        )

    await transition(
        job, JobStatus.DELIVERING, now=_now(), ttl_seconds=config.result_ttl_seconds
    )
    run.moved(JobStatus.DELIVERING)
    deliver: Deliver = ctx.get("deliver", deliver_nothing)
    started = time.monotonic()
    delivered = await deliver(job, STAGE1, config)
    run.deliver_ms = _ms_since(started)
    if delivered:
        await transition(
            job,
            JobStatus.DONE,
            now=_now(),
            reason=label,
            ttl_seconds=config.result_ttl_seconds,
        )
        run.moved(JobStatus.DONE, label)


async def _transcribe(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    scope: TenantScope,
    attempt: int,
    run: CallRun,
) -> Transcript | None:
    """Download, charge the seconds, transcribe. None when the job was
    failed, paused or scheduled to retry instead."""
    settings = get_settings()
    transcriber: Transcriber = ctx["transcriber"]
    meta = job.metadata
    started = time.monotonic()
    transcriptions = 0
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
            allow_local=settings.call_demo_allow_local_audio,
        ) as audio:
            run.download_ms, run.bytes = _ms_since(started), audio.size_bytes
            seconds = int(str(meta["duration_seconds"]))
            try:
                await charge_audio_seconds(scope, seconds)
            except CallsBudgetPaused as paused:
                await _pause(job, paused.reason_code, run=run)
                return None
            count = await start_transcription(job, now=_now())
            if count is None:
                return None
            transcriptions = count
            run.moved(JobStatus.TRANSCRIBING)
            metrics.AUDIO_SECONDS_PROCESSED.inc(seconds)
            hint = meta.get("language_hint")
            started = time.monotonic()
            spend = current_spend()
            try:
                run.transcript = await transcriber.transcribe(
                    audio.path, language_hint=hint if isinstance(hint, str) else None
                )
            except TranscriptionError:
                # Possibly billed, by a model it never named: unpriced, not free.
                if spend is not None:
                    spend.record_audio(_UNKNOWN_MODEL, seconds)
                raise
            finally:
                run.transcribe_ms = _ms_since(started)
            if spend is not None:
                spend.record_audio(run.transcript.model, seconds)
            return run.transcript
    except AudioDownloadError as refused:
        if run.download_ms is None:
            run.download_ms = _ms_since(started)
        await _download_failed(ctx, job, config, refused, attempt, run)
        return None
    except TranscriptionError as failed:
        await _transcription_failed(
            ctx, job, config, failed, attempt, transcriptions, run
        )
        return None


async def _transcription_failed(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    failed: TranscriptionError,
    attempt: int,
    transcriptions: int,
    run: CallRun,
) -> None:
    """A failure the same audio cannot get past fails the job. One that may
    pass is retried once while an attempt is left; else it is dead-lettered."""
    if not failed.retryable:
        await _fail(ctx, job, config, failed.reason, dead=False, run=run)
        return
    if (
        transcriptions >= TRANSCRIPTION_TRIES
        or attempt >= get_settings().call_max_tries
    ):
        await _fail(ctx, job, config, failed.reason, dead=True, run=run)
        return
    # CLAUDE.md never-retry overridden by the lead for this case only (BRD B6).
    await transition(
        job,
        JobStatus.QUEUED,
        now=_now(),
        reason=failed.reason,
        ttl_seconds=config.result_ttl_seconds,
    )
    run.moved(JobStatus.QUEUED, failed.reason)
    raise Retry(defer=RETRY_DELAY_SECONDS * attempt)


async def _download_failed(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    refused: AudioDownloadError,
    attempt: int,
    run: CallRun,
) -> None:
    """A refused link fails the job; one that may work later is retried until
    the attempts run out, then dead-lettered with its reason."""
    if not refused.retryable:
        await _fail(ctx, job, config, refused.reason, dead=False, run=run)
        return
    if attempt >= get_settings().call_max_tries:
        await _fail(ctx, job, config, refused.reason, dead=True, run=run)
        return
    await transition(
        job,
        JobStatus.QUEUED,
        now=_now(),
        reason=refused.reason,
        ttl_seconds=config.result_ttl_seconds,
    )
    run.moved(JobStatus.QUEUED, refused.reason)
    raise Retry(defer=RETRY_DELAY_SECONDS * attempt)


async def _fail(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    reason: str,
    *,
    dead: bool,
    run: CallRun | None = None,
) -> None:
    """failed or dead_letter with `reason`, then call.failed to the CRM."""
    status = JobStatus.DEAD_LETTER if dead else JobStatus.FAILED
    moved = await transition(
        job, status, now=_now(), reason=reason, ttl_seconds=config.result_ttl_seconds
    )
    if not moved:
        return
    if run is not None:
        run.moved(status, reason)
    deliver: Deliver = ctx.get("deliver", deliver_nothing)
    await deliver(job, FAILED, config)


async def _pause(job: Job, reason: str, *, run: CallRun | None = None) -> None:
    """paused_budget, and back on the job's queue after the cost window."""
    settings = get_settings()
    count = await pause(job, now=_now(), reason=reason)
    if count is None:
        return
    if run is not None:
        run.moved(JobStatus.PAUSED_BUDGET, reason)
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


def _log_outcome(run: CallRun) -> None:
    """ONE line per task run, and its metrics (register item 54). Numbers,
    ids and fixed words only -- never the link, a hash or a word said.
    `pass_tokens` is null until an analysis pass exists to spend any."""
    assert run.job is not None
    job, transcript = run.job, run.transcript
    status = run.status or job.status.value
    elapsed = time.monotonic() - run.started
    fields: dict[str, object] = {
        "status": status,
        "reason": run.reason,
        "attempts": run.attempts,
        "queue": job.queue,
        "duration_seconds": job.metadata.get("duration_seconds"),
        "bytes": run.bytes,
        "language_profile": None if transcript is None else transcript.language_profile,
        "uncertain": None if transcript is None else transcript.uncertain,
        "download_ms": run.download_ms,
        "transcribe_ms": run.transcribe_ms,
        "analyse_ms": run.analyse_ms,
        "deliver_ms": run.deliver_ms,
        "elapsed_ms": int(elapsed * 1000),
        "provider": None if transcript is None else transcript.provider,
        "model": None if transcript is None else transcript.model,
        "pass_tokens": None,
    }
    spend = current_spend()
    if spend is not None:
        fields.update(spend.fields())
        spend.count(status)
    level = logging.INFO if status not in _FAILURES else logging.WARNING
    _logger.log(level, "call_job_outcome", extra=fields)
    metrics.CALL_JOBS.labels(status=status).inc()
    metrics.CALL_JOB_SECONDS.observe(elapsed)
    for stage, ms in (
        ("download", run.download_ms),
        ("transcribe", run.transcribe_ms),
        ("analyse", run.analyse_ms),
        ("deliver", run.deliver_ms),
    ):
        if ms is not None:
            metrics.CALL_STAGE_SECONDS.labels(stage=stage).observe(ms / 1000)


# What a failed transcription's seconds are recorded under: no price has it.
_UNKNOWN_MODEL = "unknown"

# Statuses whose outcome line is a WARNING, not INFO.
_FAILURES = frozenset({JobStatus.FAILED.value, JobStatus.DEAD_LETTER.value, "error"})
