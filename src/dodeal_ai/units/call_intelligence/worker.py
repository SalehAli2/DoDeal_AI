"""process_call: one call job, from the queue to stage 1 (register items 35, 36,
and wave 1).

THE ORDER, and what each step may cost:

  1. The job, and the tenant's rules as they are NOW. A job that is gone or
     terminal is left alone, but for its callback when still pending: the last
     run died before it went, so it is sent again. A tenant that switched calls
     off since the push fails the job (calls_not_enabled) before any spend --
     but only once the store itself says off: the push was admitted with calls
     on, so a cached "off" is read again, and an unreadable store is a retry.
  2. At CALL_MAX_TRIES attempts the job is dead-lettered with its last reason.
  3. FREE OUTCOMES FIRST (item 36): voicemail, no answer, or shorter than
     min_transcribe_seconds is `done` with that outcome label and no paid call.
  4. A STORED TRANSCRIPT IS NEVER RE-PAID: a run that finds one delivers it.
     A run that finds the job `transcribing` with nothing stored was
     interrupted: it is transcribed again ONCE (BRD B6, the lead's override of
     CLAUDE.md's never-retry rule for this case only), and the second
     interruption dead-letters (transcription_interrupted).
  5. The call budgets (core(105)): over one, or with the store down, the job
     PAUSES. Over budget it waits out the cost window; a store outage retries
     after 300 s, doubling per outage to 3600 s. A pause never costs an
     attempt, even one taken at the charge. Nothing is spent blind.
  6. One attempt is claimed, the recording downloaded (core/audio_download.py),
     probed, inspected and converted (audio.py) before any paid call: a file
     ffprobe cannot decode, over 3600 s, unreadable, or not two channels under
     a stereo setting fails it, unpaid. Every engine is sent 16 kHz mono FLAC:
     the call's one track, or each side of a stereo call. Its seconds are
     charged -- twice for stereo, whose two sides are each transcribed, each
     side kept once it exists -- then transcribed; poor audio makes the
     transcript uncertain whatever the engine says. A transcription that failed
     and may succeed later is retried ONCE, as an attempt, its seconds charged
     again; the second failure is dead_letter and call.failed. A download that
     may succeed later is retried until the attempts run out.
  7. The transcript is kept in the job's work (core/jobs.py) the moment it
     exists, and the job moves to `analysing` for wave 1 (analysis.py): the
     language and the two passes (analysis), and the signals block -- talk
     signals, numbers, alarm phrases and their escalations -- found in code. A
     pass that fails leaves analysis null with its reason; the transcript and
     the signals block still go. A run that finds a kept transcript resumes wave 1
     there, with no attempt spent and nothing received paid for again. A
     transcript with no segments is uncertain (no_speech) and wave 1 is not
     run: no model is called, and stage 1 goes with analysis null, no_speech.
  8. The stage-1 result is stored for result_ttl_seconds and the job is
     `done`, its delivery pending when the tenant has a callback (item 50);
     call.stage1 is then delivered, and what came of it is the delivery field,
     never the status. eligible_for_full_analysis is at least
     scoring_min_seconds long and not uncertain.
  9. STAGE 2 WAITS FOR STAGE 1. Only once call.stage1 has gone is an eligible
     job's analyse_stage2 put on its own queue (stage2.py); its stage2 field,
     set with the move to done, is pending, and not_eligible otherwise. A
     queue that cannot be reached re-runs this task, which finds the job done
     and queues stage 2 then; nothing about stage 1 waits on it.

THE DEADLINE: a run stops itself at CALL_JOB_TIMEOUT_SECONDS less 60, before
arq's own cancel. The step it cut off is failed as retryable: a download or a
transcription by the rules above, anything later by a re-run of the job.

The arq job carries only (tenant, job_id); everything else is read from db3.
Nothing here logs the link, a hash, a voiceprint or a word of the transcript.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import redis
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
    DeliveryState,
    Job,
    JobStatus,
    JobStoreUnavailable,
    Stage2State,
    claim_attempt,
    clear_work,
    note_wake,
    pause,
    read_job,
    read_result,
    read_work,
    start_transcription,
    store_result,
    store_work,
    transition,
)
from dodeal_ai.core.llm import LLMClient, routed
from dodeal_ai.core.logging_config import job_log_context
from dodeal_ai.units.call_intelligence.analysis import Wave1, wave1
from dodeal_ai.units.call_intelligence.audio import (
    MAX_CALL_SECONDS,
    AudioError,
    AudioQuality,
    AudioTools,
    merge_sides,
    side_roles,
)
from dodeal_ai.units.call_intelligence.config import CallsConfig, resolve_calls_config
from dodeal_ai.units.call_intelligence.paid import JobGone, PassUsage
from dodeal_ai.units.call_intelligence.queues import enqueue_call, enqueue_stage2
from dodeal_ai.units.call_intelligence.reanalysis import (
    NO_TRANSCRIPT as REANALYSIS_NO_TRANSCRIPT,
)
from dodeal_ai.units.call_intelligence.reanalysis import (
    TRANSCRIPT_WORK,
    is_reanalysis,
    stage_wanted,
)
from dodeal_ai.units.call_intelligence.transcriber import (
    DEFAULT_STT_PROFILE,
    NO_SPEECH,
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

# A cost-store outage's pause: 300 s, doubling per outage, never over 3600 s.
# Only an over-budget pause waits out the whole cost window (core(105)).
OUTAGE_DELAY_SECONDS = 300
OUTAGE_DELAY_CAP_SECONDS = 3600
STORE_DOWN = "cost_store_unavailable"

# process_call's own deadline is arq's job timeout less this: the time left to
# record the interrupted path before arq cancels the run and records nothing.
DEADLINE_MARGIN_SECONDS = 60
DEADLINE = "job_deadline_exceeded"

# The outcome labels a call can be done with before any paid call.
_UNPAID_OUTCOMES = frozenset({"voicemail", "no_answer"})
TOO_SHORT = "too_short"

# The work field a transcript is kept under until stage 1 is stored, and the
# analysis_reason of a call done with no transcript at all.
TRANSCRIPT = TRANSCRIPT_WORK
NO_TRANSCRIPT = "no_transcript"
# The work field the audio's numbers are kept under, and a stereo side's
# transcript's prefix (`transcript:agent`), so no side is paid for twice.
AUDIO = "audio"
SIDE = "transcript:"

# The permanent failures the audio layer adds before any paid call.
TOO_LONG = "audio_too_long"
CHANNELS_MISMATCH = "audio_channels_mismatch"
NO_AUDIO_TOOLS = "audio_tools_unavailable"
# A re-analysis that asked for stage 2 alone: stage 1 kept its signals only.
STAGE1_NOT_REQUESTED = "stage1_not_requested"
# A tenant's STT profile this worker built no transcriber for.
STT_PROFILE_MISSING = "stt_profile_not_configured"
MONO = "mono"

# Delivers one event for a job and settles its delivery field; True once the
# CRM has it (or there is no callback), False when left to the schedule.
type Deliver = Callable[[Job, str, CallsConfig], Awaitable[bool]]


async def deliver_nothing(job: Job, event: str, config: CallsConfig) -> bool:
    """The deliverer before callbacks exist: nothing to send, nothing pending."""
    return True


def _now() -> datetime:
    return datetime.now(UTC)


def tenant_client(ctx: dict[str, Any], config: CallsConfig) -> LLMClient | None:
    """The worker's model client through the tenant's model route; None with
    no client at all (the pass then records llm_not_configured)."""
    client: LLMClient | None = ctx.get("llm")
    return None if client is None else routed(client, config.model_route)


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


def eligible_for_full_analysis(
    job: Job, config: CallsConfig, transcript: Transcript | None
) -> bool:
    """A transcript, not uncertain, of a call at least scoring_min_seconds."""
    duration = int(str(job.metadata["duration_seconds"]))
    return (
        transcript is not None
        and not transcript.uncertain
        and duration >= config.scoring_min_seconds
    )


def stage1_result(
    job: Job,
    config: CallsConfig,
    *,
    transcript: Transcript | None,
    outcome_label: str | None,
    wave: Wave1 | None = None,
    audio: AudioQuality | None = None,
) -> dict[str, object]:
    """The stage-1 result, held in db3 and delivered as call.stage1: the
    transcript, the audio's numbers, the roles and languages blocks, the
    signals block, wave 1's analysis (or null and why) and the versions. No wave 1 -- no transcript, or one with no
    speech -- and the signals and versions are null too; audio is null when
    the recording was not inspected."""
    duration = int(str(job.metadata["duration_seconds"]))
    return {
        "stage": 1,
        "call_id": job.call_id,
        "duration_seconds": duration,
        "outcome_label": outcome_label,
        "eligible_for_full_analysis": eligible_for_full_analysis(
            job, config, transcript
        ),
        "transcript": None
        if transcript is None
        else transcript.model_dump(mode="json"),
        "audio": None if audio is None else audio.to_dict(),
        "roles": None if wave is None else wave.roles,
        "languages": None if wave is None else wave.languages,
        "signals": None if wave is None else wave.signals,
        "analysis": None if wave is None else wave.analysis,
        "analysis_reason": _unanalysed(transcript) if wave is None else wave.reason,
        "versions": None if wave is None else wave.versions,
    }


def _unanalysed(transcript: Transcript | None) -> str:
    """Why wave 1 did not run: no transcript, or one with no speech in it."""
    return NO_TRANSCRIPT if transcript is None else NO_SPEECH


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
    usage: PassUsage = field(default_factory=PassUsage)
    analysis_reason: str | None = None
    audio: AudioQuality | None = None

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
            await _within_deadline(ctx, tenant, job_id, run, fields)
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


def deadline_seconds() -> int:
    """How long one run may take before it stops itself."""
    return get_settings().call_job_timeout_seconds - DEADLINE_MARGIN_SECONDS


async def _within_deadline(
    ctx: dict[str, Any],
    tenant: str,
    job_id: str,
    run: CallRun,
    fields: dict[str, str],
) -> None:
    """_process under the run's deadline; past it, the interrupted path."""
    deadline = asyncio.timeout(deadline_seconds())
    try:
        async with deadline:
            await _process(ctx, tenant, job_id, run, fields)
    except TimeoutError:
        if not deadline.expired():
            raise
        await _expired(ctx, tenant, job_id, run)


async def _calls_config_now(tenant: str) -> CallsConfig:
    """The call rules as the store holds them now, past the process cache: the
    push was admitted with calls on, so a stale cached "off" never ends a call.
    An unreadable store runs the job again later, spending nothing."""
    try:
        return await resolve_calls_config(tenant, fresh=True)
    except redis.RedisError:
        raise Retry(defer=RETRY_DELAY_SECONDS) from None


async def _expired(ctx: dict[str, Any], tenant: str, job_id: str, run: CallRun) -> None:
    """The deadline cut a step off: a download or a transcription fails as
    retryable under its own rules; any other running step, or a callback still
    owed, is left to a re-run of the job."""
    _logger.warning("call_job_deadline_exceeded", extra={"reason_code": DEADLINE})
    job = await read_job(tenant, job_id)
    if job is None or (
        job.terminal
        and job.delivery is not DeliveryState.PENDING
        and job.stage2 is not Stage2State.PENDING
    ):
        return
    config = await resolve_calls_config(tenant)
    if job.status is JobStatus.DOWNLOADING:
        cut = AudioDownloadError(DEADLINE, retryable=True)
        await _download_failed(ctx, job, config, cut, job.attempts, run)
        return
    if job.status is JobStatus.TRANSCRIBING:
        stalled = TranscriptionError(DEADLINE, retryable=True)
        await _transcription_failed(
            ctx, job, config, stalled, job.attempts, job.transcriptions, run
        )
        return
    raise Retry(defer=RETRY_DELAY_SECONDS)


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
        if job.delivery is DeliveryState.PENDING:
            config = await resolve_calls_config(tenant)
            event = STAGE1 if job.status is JobStatus.DONE else FAILED
            await _deliver(ctx, job, event, config, run)
        if job.stage2 is Stage2State.PENDING:
            await _queue_stage2(job)
        return
    config = await resolve_calls_config(tenant)
    if not config.calls_enabled:
        config = await _calls_config_now(tenant)
    if not config.calls_enabled:
        await _fail(ctx, job, config, "calls_not_enabled", dead=False, run=run)
        return
    label = unpaid_outcome(job, config)
    stored = None if label is not None else await read_result(tenant, job_id)
    work = (
        {}
        if label is not None or stored is not None
        else await read_work(tenant, job_id)
    )
    # A transcript wave 1 has not finished with: resumed without an attempt.
    kept = work.get(TRANSCRIPT)
    if is_reanalysis(job) and label is None and stored is None and kept is None:
        # Never the audio: a re-analysis resumes from its transcript or fails.
        await _fail(ctx, job, config, REANALYSIS_NO_TRANSCRIPT, dead=False, run=run)
        return
    paid = label is None and stored is None and kept is None
    # `transcribing` with nothing stored: the last run died mid-transcription.
    interrupted = paid and job.status is JobStatus.TRANSCRIBING
    if kept is None and (
        job.attempts >= settings.call_max_tries
        or (interrupted and job.transcriptions >= TRANSCRIPTION_TRIES)
    ):
        reason = INTERRUPTED if interrupted else job.reason or "max_tries_exceeded"
        await _fail(ctx, job, config, reason, dead=True, run=run)
        return

    scope = job_scope(job)
    if paid or kept is not None:
        try:
            await calls_budget_preflight(scope)
        except CallsBudgetPaused as paused:
            await _pause(job, paused.reason_code, run=run)
            return

    if kept is not None:
        run.transcript = Transcript.model_validate(kept)
        found = work.get(AUDIO)
        run.audio = None if found is None else AudioQuality.from_dict(found)
        if await _stage1(ctx, job, config, run.transcript, work, scope, run):
            await _done(ctx, job, config, None, run)
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
        transcript = await _transcribe(ctx, job, config, scope, attempt, run, work)
        if transcript is None:
            return
        await store_work(
            tenant,
            job_id,
            TRANSCRIPT,
            transcript.model_dump(mode="json"),
            ttl_seconds=config.result_ttl_seconds,
        )
        if not await _stage1(ctx, job, config, transcript, {}, scope, run):
            return
    await _done(ctx, job, config, label, run)


async def _stage1(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    transcript: Transcript,
    work: dict[str, dict[str, object]],
    scope: TenantScope,
    run: CallRun,
) -> bool:
    """`analysing`, wave 1, and the stage-1 result stored. False when the job
    went terminal or expired under it: there is nothing left to finish."""
    if not await transition(
        job, JobStatus.ANALYSING, now=_now(), ttl_seconds=config.result_ttl_seconds
    ):
        return False
    run.moved(JobStatus.ANALYSING)
    wave: Wave1 | None = None
    if not transcript.segments:
        # Nothing heard: nothing for a pass to read, so no model is called.
        transcript = transcript.doubted(NO_SPEECH)
        run.analysis_reason = NO_SPEECH
    else:
        started = time.monotonic()
        stage1 = stage_wanted(job, 1)
        try:
            wave = await wave1(
                tenant_client(ctx, config) if stage1 else None,
                job,
                config,
                transcript,
                work=work,
                scope=scope,
                settings=get_settings(),
                usage=run.usage,
            )
        except JobGone:
            return False
        finally:
            run.analyse_ms = _ms_since(started)
        if not stage1:
            wave = replace(wave, reason=STAGE1_NOT_REQUESTED)
        transcript, run.analysis_reason = wave.transcript, wave.reason
    run.transcript = transcript
    result = stage1_result(
        job,
        config,
        transcript=transcript,
        outcome_label=None,
        wave=wave,
        audio=run.audio,
    )
    await store_result(
        job.tenant, job.job_id, result, ttl_seconds=config.result_ttl_seconds
    )
    await clear_work(job.tenant, job.job_id)
    return True


async def _done(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    label: str | None,
    run: CallRun,
) -> None:
    """`done` with its delivery owed and its stage-2 state, then call.stage1
    sent once, and only then stage 2 queued for an eligible call."""
    eligible = eligible_for_full_analysis(job, config, run.transcript)
    wanted = eligible and stage_wanted(job, 2)
    stage2 = Stage2State.PENDING if wanted else Stage2State.NOT_ELIGIBLE
    stage1 = stage_wanted(job, 1)
    await transition(
        job,
        JobStatus.DONE,
        now=_now(),
        reason=label,
        ttl_seconds=config.result_ttl_seconds,
        delivery=owed_delivery(config) if stage1 else None,
        stage2=stage2,
    )
    run.moved(JobStatus.DONE, label)
    if stage1:
        await _deliver(ctx, job, STAGE1, config, run)
    if stage2 is Stage2State.PENDING:
        await _queue_stage2(job)


async def _queue_stage2(job: Job) -> None:
    """analyse_stage2 on its own queue, once per job. A queue that cannot be
    reached runs this task again soon, which finds the job done and tries
    again: stage 1 has gone already, and is never held for stage 2."""
    try:
        await enqueue_stage2(job.tenant, job.job_id)
    except QueueUnavailable:
        raise Retry(defer=RETRY_DELAY_SECONDS) from None


def owed_delivery(config: CallsConfig) -> DeliveryState | None:
    """pending when the tenant has a callback URL to send the event to."""
    return None if config.callback_url is None else DeliveryState.PENDING


async def _deliver(
    ctx: dict[str, Any], job: Job, event: str, config: CallsConfig, run: CallRun
) -> None:
    """One inline attempt at `event`; the deliverer settles the field."""
    deliver: Deliver = ctx.get("deliver", deliver_nothing)
    started = time.monotonic()
    await deliver(job, event, config)
    run.deliver_ms = _ms_since(started)


async def _transcribe(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    scope: TenantScope,
    attempt: int,
    run: CallRun,
    work: dict[str, dict[str, object]],
) -> Transcript | None:
    """Download, inspect, charge the seconds, transcribe. None when the job
    was failed, paused or scheduled to retry instead."""
    settings = get_settings()
    meta = job.metadata
    seconds = int(str(meta["duration_seconds"]))
    tools: AudioTools | None = ctx.get("audio")
    stereo = config.audio_channels != MONO
    refusal = (
        TOO_LONG
        if seconds > MAX_CALL_SECONDS
        else NO_AUDIO_TOOLS
        if stereo and tools is None
        else None
    )
    if refusal is not None:
        await _fail(ctx, job, config, refusal, dead=False, run=run)
        return None
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
            if tools is not None:
                await tools.probe(audio.path)
                run.audio = await _inspected(tools, audio.path, stereo)
                await store_work(
                    job.tenant,
                    job.job_id,
                    AUDIO,
                    run.audio.to_dict(),
                    ttl_seconds=config.result_ttl_seconds,
                )
            async with _engine_audio(tools, audio.path, stereo) as files:
                try:
                    await charge_audio_seconds(scope, seconds * len(files))
                except CallsBudgetPaused as paused:
                    await _pause(job, paused.reason_code, claimed=True, run=run)
                    run.attempts = attempt - 1
                    return None
                count = await start_transcription(job, now=_now())
                if count is None:
                    return None
                transcriptions = count
                run.moved(JobStatus.TRANSCRIBING)
                metrics.AUDIO_SECONDS_PROCESSED.inc(seconds)
                length = call_length(seconds, run.audio)
                started = time.monotonic()
                try:
                    if stereo:
                        transcript = await _two_sides(
                            ctx, job, config, files, work, length
                        )
                    else:
                        transcript = await _paid(ctx, job, config, files[0], length)
                finally:
                    run.transcribe_ms = _ms_since(started)
            if run.audio is not None:
                transcript = transcript.doubted(*run.audio.reasons())
            run.transcript = transcript
            return transcript
    except AudioDownloadError as refused:
        if run.download_ms is None:
            run.download_ms = _ms_since(started)
        await _download_failed(ctx, job, config, refused, attempt, run)
        return None
    except AudioError as refused:
        await _fail(ctx, job, config, refused.reason, dead=False, run=run)
        return None
    except TranscriptionError as failed:
        await _transcription_failed(
            ctx, job, config, failed, attempt, transcriptions, run
        )
        return None


async def _inspected(tools: AudioTools, path: Path, stereo: bool) -> AudioQuality:
    """The recording's numbers, or AudioError for one too long, or not two
    channels under a stereo setting: refused before any paid call."""
    quality = await tools.inspect(path)
    if quality.duration_seconds > MAX_CALL_SECONDS:
        raise AudioError(TOO_LONG)
    if stereo and quality.channels != 2:
        raise AudioError(CHANNELS_MISMATCH)
    return quality


def call_length(declared: int, audio: AudioQuality | None) -> float:
    """The call's length an engine is told: the larger of the push's and
    ffmpeg's, so a limit on length is never passed on either's word."""
    measured = 0.0 if audio is None else audio.duration_seconds
    return max(float(declared), measured)


@asynccontextmanager
async def _engine_audio(
    tools: AudioTools | None, path: Path, stereo: bool
) -> AsyncIterator[tuple[Path, ...]]:
    """What the engine is sent, made before any charge: one 16 kHz mono FLAC
    file, or one per side for stereo. Only the demo without ffmpeg (mono, as
    the worker's start allows) sends the download as it came."""
    if tools is None:
        yield (path,)
    elif stereo:
        async with tools.split(path) as sides:
            yield sides
    else:
        async with tools.converted(path) as flac:
            yield (flac,)


def transcriber_for(ctx: dict[str, Any], profile: str) -> Transcriber:
    """The transcriber of the tenant's STT profile; a profile this worker has
    none for fails the call, unpaid, never falling back to another engine."""
    if profile == DEFAULT_STT_PROFILE:
        transcriber: Transcriber = ctx["transcriber"]
        return transcriber
    named: Transcriber | None = ctx.get("transcribers", {}).get(profile)
    if named is None:
        raise TranscriptionError(STT_PROFILE_MISSING, retryable=False)
    return named


async def _paid(
    ctx: dict[str, Any], job: Job, config: CallsConfig, path: Path, length: float
) -> Transcript:
    """One paid transcription of `path`, a call `length` seconds long, its
    seconds recorded on the spend under the model that answered, or as
    unpriced when none did."""
    transcriber = transcriber_for(ctx, config.stt_profile)
    hint = job.metadata.get("language_hint")
    seconds = int(str(job.metadata["duration_seconds"]))
    spend = current_spend()
    try:
        transcript = await transcriber.transcribe(
            path,
            language_hint=hint if isinstance(hint, str) else None,
            duration_seconds=length,
        )
    except (TranscriptionError, asyncio.CancelledError):
        # Possibly billed, by a model it never named: unpriced, not free, and
        # its tokens unknown.
        if spend is not None:
            spend.record_audio(_UNKNOWN_MODEL, seconds)
            spend.record_stt_usage(None, None)
        raise
    if spend is not None:
        spend.record_audio(transcript.model, seconds)
    return transcript


async def _two_sides(
    ctx: dict[str, Any],
    job: Job,
    config: CallsConfig,
    files: tuple[Path, ...],
    work: dict[str, dict[str, object]],
    length: float,
) -> Transcript:
    """Each side of a stereo call transcribed on its own and merged by time,
    a side kept the moment it exists so a retry never pays for it again."""
    roles = side_roles(config.audio_channels)
    sides: list[tuple[str, Transcript]] = []
    for role, side_path in zip(roles, files, strict=True):
        kept = work.get(f"{SIDE}{role}")
        if kept is not None:
            sides.append((role, Transcript.model_validate(kept)))
            continue
        side = await _paid(ctx, job, config, side_path, length)
        await store_work(
            job.tenant,
            job.job_id,
            f"{SIDE}{role}",
            side.model_dump(mode="json"),
            ttl_seconds=config.result_ttl_seconds,
        )
        sides.append((role, side))
    first = sides[0][1]
    return merge_sides(sides, provider=first.provider, model=first.model)


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
        job,
        status,
        now=_now(),
        reason=reason,
        ttl_seconds=config.result_ttl_seconds,
        delivery=owed_delivery(config),
    )
    if not moved:
        return
    if run is not None:
        run.moved(status, reason)
    deliver: Deliver = ctx.get("deliver", deliver_nothing)
    await deliver(job, FAILED, config)


def pause_delay(reason: str, outages: int, *, window_seconds: int) -> int:
    """Seconds until a paused job runs again: the cost window when over
    budget, else 300 s doubled per outage so far, capped at 3600 s."""
    if reason != STORE_DOWN:
        return window_seconds
    # The exponent's own cap only keeps the number small; the delay cap decides.
    doublings = min(max(outages - 1, 0), 8)
    return min(OUTAGE_DELAY_SECONDS * 2**doublings, OUTAGE_DELAY_CAP_SECONDS)


async def _pause(
    job: Job, reason: str, *, claimed: bool = False, run: CallRun | None = None
) -> None:
    """paused_budget, scored at its wake-up for the sweep, and back on the
    job's queue after pause_delay. `claimed` gives back the attempt this run
    took: a pause never costs one."""
    settings = get_settings()
    counts = await pause(
        job, now=_now(), reason=reason, claimed=claimed, outage=reason == STORE_DOWN
    )
    if counts is None:
        return
    pauses, outages = counts
    if run is not None:
        run.moved(JobStatus.PAUSED_BUDGET, reason)
    delay = pause_delay(reason, outages, window_seconds=settings.cost_window_seconds)
    await note_wake(job, wake_at=_now() + timedelta(seconds=delay))
    try:
        await enqueue_call(
            job.tenant,
            job.job_id,
            job.queue,
            resume=pauses,
            defer=timedelta(seconds=delay),
        )
    except QueueUnavailable:
        # Paused with nothing to wake it: run this again soon, which pauses
        # again and queues the resume then.
        raise Retry(defer=RETRY_DELAY_SECONDS) from None


def _log_outcome(run: CallRun) -> None:
    """ONE line per task run, and its metrics (register item 54). Numbers,
    ids and fixed words only -- never the link, a hash or a word said.
    `pass_tokens` is each pass's tokens in this run, and
    `pass_reasoning_tokens` the reasoning part of each, null when none ran."""
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
        "speech_ratio": None if run.audio is None else run.audio.speech_ratio,
        "mean_volume_db": None if run.audio is None else run.audio.mean_volume_db,
        "download_ms": run.download_ms,
        "transcribe_ms": run.transcribe_ms,
        "analyse_ms": run.analyse_ms,
        "deliver_ms": run.deliver_ms,
        "elapsed_ms": int(elapsed * 1000),
        "provider": None if transcript is None else transcript.provider,
        "model": None if transcript is None else transcript.model,
        "pass_tokens": run.usage.tokens or None,
        "pass_reasoning_tokens": run.usage.reasoning or None,
        "evidence_dropped": run.usage.dropped or None,
        "evidence_unverified": run.usage.unverified or None,
        "analysis_reason": run.analysis_reason,
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
