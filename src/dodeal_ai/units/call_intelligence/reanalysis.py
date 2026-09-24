"""Re-analysing a stored call (Unit B): its stage-1 transcript, pushed back by
the CRM, run again with the versions in force now -- after the objection list,
the checklist or a prompt changed.

A JOB OF ITS OWN, kind reanalysis, on the overnight queue: never the call's
first job, whose results are never touched. One per (tenant, call, stages,
versions), under its own index (core/jobs.reanalysis_index_key), so the same
push twice is one job and a push after a version moved is a new one.

NEVER THE AUDIO: the transcript is kept as the job's work before it is queued,
so the worker resumes from it and the transcriber is never called; a job that
has none fails, unpaid (worker.py). Stage 1 runs through the roles pass and
wave 1 when asked for; stage 2 when asked for and the call is eligible. Both
go through the tenant's model route, and every event says reanalysis: true.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime

from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.errors import CallsNotEnabled, JobStoreUnavailableResponse
from dodeal_ai.core.jobs import (
    Job,
    JobStatus,
    JobStoreUnavailable,
    create_job,
    read_job,
    reanalysis_index_key,
    store_work,
)
from dodeal_ai.units.call_intelligence.coaching import TONE_LIST_VERSION
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.objections import OBJECTION_LIST_VERSION
from dodeal_ai.units.call_intelligence.prompts import PROMPT_SET_VERSION
from dodeal_ai.units.call_intelligence.queues import OVERNIGHT_QUEUE, enqueue_call
from dodeal_ai.units.call_intelligence.schemas import (
    CallJobAccepted,
    ReanalysisRequest,
)
from dodeal_ai.units.call_intelligence.score import RUBRIC_VERSION
from dodeal_ai.units.call_intelligence.signals import SIGNALS_VERSION

_logger = logging.getLogger("dodeal_ai.unit_b")

REANALYSIS = "reanalysis"

# The work field a job's transcript is kept under (worker.py reads it).
TRANSCRIPT_WORK = "transcript"

# Why a re-analysis job failed: it had no transcript to resume from.
NO_TRANSCRIPT = "reanalysis_transcript_missing"

# Every stage, for a job that names none (a call's own).
ALL_STAGES = (1, 2)


def is_reanalysis(job: Job) -> bool:
    return job.metadata.get("kind") == REANALYSIS


def stage_wanted(job: Job, stage: int) -> bool:
    """Whether the job runs `stage`: always for a call's own job."""
    stages = job.metadata.get("stages", ALL_STAGES)
    return isinstance(stages, list | tuple) and stage in stages


def current_versions(config: CallsConfig) -> dict[str, object]:
    """What a re-analysis runs on: the code's versions and the tenant's rules."""
    return {
        "prompt": PROMPT_SET_VERSION,
        "objection_list": OBJECTION_LIST_VERSION,
        "rubric": RUBRIC_VERSION,
        "tone_list": TONE_LIST_VERSION,
        "signals": SIGNALS_VERSION,
        "config": config.config_version,
    }


def reanalysis_digest(
    call_id: int, stages: Sequence[int], versions: dict[str, object]
) -> str:
    """SHA-256 over the call, the stages and the versions: the job's identity."""
    identity = {"call_id": call_id, "stages": list(stages), "versions": versions}
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()


async def admit_reanalysis(
    scope: TenantScope,
    body: ReanalysisRequest,
    config: CallsConfig,
    *,
    now: datetime,
) -> CallJobAccepted:
    """The re-analysis job, made or found, its transcript kept and it queued
    while it is queued. Fails closed at both stores, as a push does."""
    if not config.calls_enabled:
        raise CallsNotEnabled()
    versions = current_versions(config)
    metadata = {
        **body.model_dump(mode="json", exclude={"call_id", "transcript"}),
        "kind": REANALYSIS,
        "versions": versions,
        "config_version": config.config_version,
    }
    index = reanalysis_index_key(
        scope.tenant, reanalysis_digest(body.call_id, body.stages, versions)
    )
    try:
        job_id, created = await create_job(
            scope.tenant,
            body.call_id,
            job_id=uuid.uuid4().hex,
            request_id=scope.request_id,
            queue=OVERNIGHT_QUEUE,
            metadata=metadata,
            now=now,
            index=index,
        )
        job = await read_job(scope.tenant, job_id)
        if job is not None and job.status is JobStatus.QUEUED:
            await store_work(
                scope.tenant,
                job_id,
                TRANSCRIPT_WORK,
                body.transcript.model_dump(mode="json"),
                ttl_seconds=config.result_ttl_seconds,
            )
    except JobStoreUnavailable:
        raise JobStoreUnavailableResponse() from None
    if job is None:
        raise JobStoreUnavailableResponse()
    if job.status is JobStatus.QUEUED:
        await enqueue_call(scope.tenant, job.job_id, job.queue)
    # Ids and fixed words only: never the transcript.
    _logger.info(
        "call_reanalysis_admitted",
        extra={
            "tenant": scope.tenant,
            "request_id": scope.request_id,
            "job_id": job.job_id,
            "new_job": created,
            "reason": body.reason,
            "stages": body.stages,
            "status": job.status.value,
        },
    )
    return CallJobAccepted(job_id=job.job_id, status=job.status)
