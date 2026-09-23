"""Delivering a job's events to the CRM, and retrying them (Unit B).

ONE ATTEMPT INLINE, THEN THE SCHEDULE. process_call delivers call.stage1 (or a
failed job's call.failed) once, straight away. A delivery that may succeed
later is re-tried by the `deliver_callback` task after 60, 300, 1800 and 7200
seconds (core/callbacks.py); after the last, the delivery has failed. A
delivery that can never succeed -- no secret, a refused address -- fails at
once. Either way a stage-1 job ends `failed` with reason delivery_failed, its
result still held for GET.

A tenant with no callback_url has nothing to deliver: the job is done and the
CRM reads the result by GET.

The body is built from db3 at every attempt: ids, the stage-1 result or the
failure's status and reason. Never the audio link, a hash or a voiceprint.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from dodeal_ai.core import metrics
from dodeal_ai.core.audio_download import resolve_host
from dodeal_ai.core.callbacks import (
    CALL_STAGE1,
    DELIVERY_DELAYS_SECONDS,
    Delivery,
    event_id,
    post_event,
)
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.jobs import Job, JobStatus, read_job, read_result, transition
from dodeal_ai.core.logging_config import job_log_context
from dodeal_ai.units.call_intelligence.config import CallsConfig, resolve_calls_config
from dodeal_ai.units.call_intelligence.queues import enqueue_delivery

_logger = logging.getLogger("dodeal_ai.unit_b")


async def event_body(job: Job, event: str) -> bytes:
    """The event's JSON body, from the job as it is now in db3."""
    body: dict[str, object] = {
        "event": event,
        "event_id": event_id(job.tenant, job.job_id, event),
        "job_id": job.job_id,
        "call_id": job.call_id,
        "lead_id": job.metadata.get("lead_id"),
        "author_id": job.metadata.get("author_id"),
    }
    if event == CALL_STAGE1:
        body["result"] = await read_result(job.tenant, job.job_id)
    else:
        body["status"] = job.status.value
        body["reason"] = job.reason
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


async def _attempt(
    ctx: dict[str, Any], job: Job, event: str, config: CallsConfig
) -> Delivery | None:
    """One signed POST of `event`, or None when the tenant has no callback."""
    if config.callback_url is None:
        return None
    current = await read_job(job.tenant, job.job_id) or job
    return await post_event(
        config.callback_url,
        tenant=job.tenant,
        event=event,
        event_id=event_id(job.tenant, job.job_id, event),
        body=await event_body(current, event),
        timestamp=str(int(time.time())),
        http=ctx["http"],
        resolve=ctx.get("resolve", resolve_host),
        allow_local=get_settings().call_demo_allow_local_audio,
    )


async def deliver_event(
    ctx: dict[str, Any], job: Job, event: str, config: CallsConfig
) -> bool:
    """process_call's deliverer: True once the CRM has the event or there is
    no callback to send; False when it is left to the schedule or has failed."""
    outcome = await _attempt(ctx, job, event, config)
    _log(job, event, outcome, attempt=0)
    if outcome is None or outcome is Delivery.DELIVERED:
        return True
    if outcome is Delivery.RETRY:
        await _schedule(job, event, attempt=1)
        return False
    await _delivery_failed(job, event, config)
    return False


async def deliver_callback(
    ctx: dict[str, Any], tenant: str, job_id: str, event: str, attempt: int
) -> None:
    """The arq task for the `attempt`-th retry of one event. Every line it
    logs names the job (register item 54)."""
    with job_log_context(tenant=tenant, job_id=job_id) as fields:
        await _retry(ctx, tenant, job_id, event, attempt, fields)


async def _retry(
    ctx: dict[str, Any],
    tenant: str,
    job_id: str,
    event: str,
    attempt: int,
    fields: dict[str, str],
) -> None:
    job = await read_job(tenant, job_id)
    if job is None:
        return
    fields["request_id"] = job.request_id
    if event == CALL_STAGE1 and job.status is not JobStatus.DELIVERING:
        return
    config = await resolve_calls_config(tenant)
    outcome = await _attempt(ctx, job, event, config)
    _log(job, event, outcome, attempt=attempt)
    if outcome is None or outcome is Delivery.DELIVERED:
        if event == CALL_STAGE1:
            result = await read_result(tenant, job_id) or {}
            label = result.get("outcome_label")
            await transition(
                job,
                JobStatus.DONE,
                now=datetime.now(UTC),
                reason=label if isinstance(label, str) else None,
                ttl_seconds=config.result_ttl_seconds,
            )
        return
    if outcome is Delivery.RETRY and attempt < len(DELIVERY_DELAYS_SECONDS):
        await _schedule(job, event, attempt=attempt + 1)
        return
    await _delivery_failed(job, event, config)


async def _schedule(job: Job, event: str, *, attempt: int) -> None:
    await enqueue_delivery(
        job.tenant,
        job.job_id,
        event,
        attempt=attempt,
        queue=job.queue,
        defer=timedelta(seconds=DELIVERY_DELAYS_SECONDS[attempt - 1]),
    )


async def _delivery_failed(job: Job, event: str, config: CallsConfig) -> None:
    """A stage-1 job ends failed/delivery_failed, its result still held; a
    call.failed that could not be sent leaves its job as it was."""
    _logger.warning(
        "callback_delivery_failed",
        extra={
            "reason_code": "delivery_failed",
            "tenant": job.tenant,
            "job_id": job.job_id,
            "event": event,
        },
    )
    if event == CALL_STAGE1:
        await transition(
            job,
            JobStatus.FAILED,
            now=datetime.now(UTC),
            reason="delivery_failed",
            ttl_seconds=config.result_ttl_seconds,
        )


def _log(job: Job, event: str, outcome: Delivery | None, *, attempt: int) -> None:
    """One line and one count per attempt: which event, which attempt, what
    came of it."""
    word = "no_callback" if outcome is None else outcome.value
    metrics.CALLBACK_DELIVERIES.labels(event=event, outcome=word).inc()
    _logger.info(
        "callback_attempt",
        extra={
            "tenant": job.tenant,
            "job_id": job.job_id,
            "event": event,
            "attempt": attempt,
            "outcome": word,
        },
    )
