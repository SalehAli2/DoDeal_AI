"""Admitting a call job (register item 50): one job per call, on the queue its
lead's status picks, for a tenant that has switched calls on.

THE ORDER: the tenant's switch (403 calls_not_enabled, nothing written), then
the job (db3, one per call), then the queue (db0). A push for a call that
already has a job is told that job and its status, and is put on the queue
again only while the job is still `queued` -- the arq id makes that a no-op
when it is already there, and the repair when an earlier push lost the queue.

FAILS CLOSED at both stores: a job that cannot be written or read is 503
job_store_unavailable, and a queue that cannot be reached is 503
queue_unavailable. Either way the CRM pushes again and is told the one job.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.errors import CallsNotEnabled, JobStoreUnavailableResponse
from dodeal_ai.core.jobs import JobStatus, JobStoreUnavailable, create_job, read_job
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.queues import enqueue_call, queue_for
from dodeal_ai.units.call_intelligence.schemas import CallJobAccepted, CallJobRequest

_logger = logging.getLogger("dodeal_ai.unit_b")


def job_metadata(body: CallJobRequest, config: CallsConfig) -> dict[str, object]:
    """What the worker needs, kept with the job in db3. The voiceprint only
    under the tenant's voice_id switch: without consent it is not kept at all."""
    metadata = body.model_dump(mode="json", exclude={"call_id", "agent_voiceprint"})
    if config.voice_id_enabled and body.agent_voiceprint is not None:
        metadata["agent_voiceprint"] = body.agent_voiceprint
    metadata["config_version"] = config.config_version
    return metadata


async def admit_call(
    scope: TenantScope,
    body: CallJobRequest,
    config: CallsConfig,
    *,
    now: datetime,
) -> CallJobAccepted:
    """The call's job, made or found, on its queue while it is queued."""
    if not config.calls_enabled:
        raise CallsNotEnabled()
    try:
        job_id, created = await create_job(
            scope.tenant,
            body.call_id,
            job_id=uuid.uuid4().hex,
            request_id=scope.request_id,
            queue=queue_for(body.lead_status, config),
            metadata=job_metadata(body, config),
            now=now,
        )
        job = await read_job(scope.tenant, job_id)
    except JobStoreUnavailable:
        raise JobStoreUnavailableResponse() from None
    if job is None:
        # The index outlived its job by the width of one expiry: nothing to
        # answer with, and nothing to guess.
        raise JobStoreUnavailableResponse()
    if job.status is JobStatus.QUEUED:
        await enqueue_call(scope.tenant, job.job_id, job.queue)
    # Ids and fixed words only: never the link, a hash or the voiceprint.
    _logger.info(
        "call_job_admitted",
        extra={
            "tenant": scope.tenant,
            "request_id": scope.request_id,
            "job_id": job.job_id,
            "queue": job.queue,
            "new_job": created,
            "status": job.status.value,
        },
    )
    return CallJobAccepted(job_id=job.job_id, status=job.status)
