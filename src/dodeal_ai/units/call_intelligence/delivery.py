"""Delivering a job's events to the CRM, and retrying them (Unit B).

ONE ATTEMPT INLINE, THEN THE SCHEDULE. process_call delivers call.stage1 (or a
failed job's call.failed) once, straight away. A delivery that may succeed
later is re-tried by the `deliver_callback` task after 60, 300, 1800 and 7200
seconds (core/callbacks.py); after the last, the delivery has failed. A
delivery that can never succeed -- no secret, a refused address -- fails at
once.

DELIVERY IS NOT STATUS (register item 50). The job's `delivery` field goes
pending -> delivered or delivery_failed; the status is never touched here, so
a job with a result stays `done` and its result is held for GET either way.
A retry for a delivery no longer pending sends nothing.

STAGE 2 HAS A DELIVERY OF ITS OWN, `stage2_delivery`, on the same schedule:
call.stage2, or call.failed for stage 2 only -- a done job whose stage 2
failed. A done job sends no other call.failed, so the event and the job's
stage-2 state together say which delivery an attempt settles.

SO HAS EACH TRANSLATION, one per target, on the same schedule: its event goes
through here as `call.translation:<target>` (translation_event), which names
its delivery field, its event id and its retries; the CRM sees the event
call.translation, its body as translation.py always sent it.

A tenant with no callback_url has nothing to deliver and no delivery field;
the CRM reads the result by GET. One whose URL was removed while a retry was
pending gets delivery_failed: there is nowhere left to send it.

The body is built from db3 at every attempt: ids, the stage-1 or stage-2
result, or the failure's status and reason. Never the audio link, a hash or a
voiceprint.
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
    CALL_FAILED,
    CALL_STAGE1,
    CALL_STAGE2,
    CALL_TRANSLATION,
    DELIVERY_DELAYS_SECONDS,
    Delivery,
    event_id,
    post_event,
)
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.jobs import (
    DELIVERY_FIELD,
    STAGE2_DELIVERY_FIELD,
    DeliveryState,
    Job,
    Stage2State,
    read_job,
    read_result,
    read_stage2_result,
    read_translation,
    settle_delivery,
    translation_delivery_field,
)
from dodeal_ai.core.logging_config import job_log_context
from dodeal_ai.units.call_intelligence.config import CallsConfig, resolve_calls_config
from dodeal_ai.units.call_intelligence.queues import enqueue_delivery
from dodeal_ai.units.call_intelligence.reanalysis import is_reanalysis

_logger = logging.getLogger("dodeal_ai.unit_b")


def stage2_event(job: Job, event: str) -> bool:
    """Whether `event` is stage 2's own: call.stage2, or call.failed for a done
    job whose stage 2 failed."""
    return event == CALL_STAGE2 or (
        event == CALL_FAILED and job.stage2 is Stage2State.FAILED
    )


def translation_event(target: str) -> str:
    """The event a translation into `target` is delivered as, here and on the
    retry queue; the CRM is sent call.translation."""
    return f"{CALL_TRANSLATION}:{target}"


def _target(event: str) -> str | None:
    """The target of a translation's event; None for any other event."""
    name, _, target = event.partition(":")
    return target if name == CALL_TRANSLATION and target else None


def _field(job: Job, event: str) -> str:
    """The job's field `event`'s delivery lives in."""
    target = _target(event)
    if target is not None:
        return translation_delivery_field(target)
    return STAGE2_DELIVERY_FIELD if stage2_event(job, event) else DELIVERY_FIELD


def _owed(job: Job, event: str) -> DeliveryState | None:
    """The delivery `event` settles: a translation's, stage 2's own, or the
    job's."""
    target = _target(event)
    if target is not None:
        return job.translation_delivery.get(target)
    return job.stage2_delivery if stage2_event(job, event) else job.delivery


async def event_body(job: Job, event: str) -> bytes:
    """The event's JSON body, from the job as it is now in db3."""
    target = _target(event)
    if target is not None:
        return await _translation_body(job, event, target)
    body: dict[str, object] = {
        "event": event,
        "event_id": event_id(job.tenant, job.job_id, event),
        "job_id": job.job_id,
        "call_id": job.call_id,
        "lead_id": job.metadata.get("lead_id"),
        "author_id": job.metadata.get("author_id"),
    }
    if is_reanalysis(job):
        body["reanalysis"] = True
    if event == CALL_STAGE1:
        body["result"] = await read_result(job.tenant, job.job_id)
    elif event == CALL_STAGE2:
        body["result"] = await read_stage2_result(job.tenant, job.job_id)
    elif stage2_event(job, event):
        # call.failed for stage 2 only: the job stays done, stage 1 stands.
        body["stage"] = 2
        body["status"] = job.status.value
        body["stage2"] = Stage2State.FAILED.value
        body["reason"] = job.stage2_reason
    else:
        body["status"] = job.status.value
        body["reason"] = job.reason
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


async def _translation_body(job: Job, event: str, target: str) -> bytes:
    """call.translation's body: the ids and the translation as held now."""
    body: dict[str, object] = {
        "event": CALL_TRANSLATION,
        "event_id": event_id(job.tenant, job.job_id, event),
        "job_id": job.job_id,
        "call_id": job.call_id,
        "result": await read_translation(job.tenant, job.job_id, target),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


async def _attempt(
    ctx: dict[str, Any], job: Job, event: str, config: CallsConfig
) -> tuple[Delivery | None, Job]:
    """One signed POST of `event`, or None when the tenant has no callback;
    and the job as the attempt read it."""
    if config.callback_url is None:
        return None, job
    current = await read_job(job.tenant, job.job_id) or job
    outcome = await post_event(
        config.callback_url,
        tenant=job.tenant,
        event=event.partition(":")[0],
        event_id=event_id(job.tenant, job.job_id, event),
        body=await event_body(current, event),
        timestamp=str(int(time.time())),
        http=ctx["http"],
        resolve=ctx.get("resolve", resolve_host),
        allow_local=get_settings().call_demo_allow_local_audio,
    )
    return outcome, current


async def _delivered(job: Job, event: str) -> None:
    """The event's delivery is delivered."""
    await settle_delivery(
        job,
        DeliveryState.DELIVERED,
        now=datetime.now(UTC),
        delivery_field=_field(job, event),
    )


async def deliver_event(
    ctx: dict[str, Any], job: Job, event: str, config: CallsConfig
) -> bool:
    """process_call's deliverer: True once the CRM has the event or there is
    no callback to send; False when it is left to the schedule or has failed."""
    outcome, job = await _attempt(ctx, job, event, config)
    _log(job, event, outcome, attempt=0)
    if outcome is None:
        return True
    if outcome is Delivery.DELIVERED:
        await _delivered(job, event)
        return True
    if outcome is Delivery.RETRY:
        await _schedule(job, event, attempt=1)
        return False
    await _delivery_failed(job, event)
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
    if _owed(job, event) is not DeliveryState.PENDING:
        return
    config = await resolve_calls_config(tenant)
    outcome, job = await _attempt(ctx, job, event, config)
    _log(job, event, outcome, attempt=attempt)
    if outcome is Delivery.DELIVERED:
        await _delivered(job, event)
        return
    if outcome is Delivery.RETRY and attempt < len(DELIVERY_DELAYS_SECONDS):
        await _schedule(job, event, attempt=attempt + 1)
        return
    await _delivery_failed(job, event)


async def _schedule(job: Job, event: str, *, attempt: int) -> None:
    await enqueue_delivery(
        job.tenant,
        job.job_id,
        event,
        attempt=attempt,
        queue=job.queue,
        defer=timedelta(seconds=DELIVERY_DELAYS_SECONDS[attempt - 1]),
    )


async def _delivery_failed(job: Job, event: str) -> None:
    """delivery_failed, the job's status and result left as they were."""
    _logger.warning(
        "callback_delivery_failed",
        extra={
            "reason_code": "delivery_failed",
            "tenant": job.tenant,
            "job_id": job.job_id,
            "event": event,
        },
    )
    await settle_delivery(
        job,
        DeliveryState.DELIVERY_FAILED,
        now=datetime.now(UTC),
        delivery_field=_field(job, event),
    )


def _log(job: Job, event: str, outcome: Delivery | None, *, attempt: int) -> None:
    """One line and one count per attempt: which event, which attempt, what
    came of it."""
    word = "no_callback" if outcome is None else outcome.value
    metrics.CALLBACK_DELIVERIES.labels(
        event=event.partition(":")[0], outcome=word
    ).inc()
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
