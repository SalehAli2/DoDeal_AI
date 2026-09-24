"""Reading a call job back (register item 50): status, reason, the callback's
delivery and wave 2's state beside the status, and the stage-1 and stage-2
results while each is held.

TENANT BY KEY. The job is looked up under the tenant the gates verified, so
another tenant's job_id is simply not there: 404 call_job_not_found, the same
answer as an id that never existed or has expired.

ONE AUDIT LINE PER READ, found or not: who read which job, and what state it
was in -- never the result, the transcript or the link.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import CallJobNotFound, JobStoreUnavailableResponse
from dodeal_ai.core.jobs import (
    DeliveryState,
    JobStatus,
    JobStoreUnavailable,
    Stage2State,
    read_job,
    read_result,
    read_stage2_result,
    read_translation,
)

_audit = logging.getLogger("dodeal_ai.audit")

# The languages a call may be translated into (translation.py).
TRANSLATION_TARGETS = ("ar", "en")


class CallJobView(BaseModel):
    """What the CRM reads: where the job is, why, whether its callback went
    (None: no callback to send), where wave 2 is (None: not done yet), and its
    results while held."""

    job_id: str
    status: JobStatus
    reason: str | None
    delivery: DeliveryState | None
    stage2: Stage2State | None
    result: dict[str, object] | None
    stage2_result: dict[str, object] | None
    # The held translations by target (translation.py); {} for none.
    translations: dict[str, object] = {}


async def read_translations(tenant: str, job_id: str) -> dict[str, object]:
    """Every held translation of the job, by target."""
    held: dict[str, object] = {}
    for target in TRANSLATION_TARGETS:
        found = await read_translation(tenant, job_id, target)
        if found is not None:
            held[target] = found
    return held


async def read_call_job(context: RequestContext, job_id: str) -> CallJobView:
    """This tenant's job, or 404; the read is audited either way."""
    try:
        job = await read_job(context.tenant, job_id)
        result = None if job is None else await read_result(context.tenant, job_id)
        stage2 = (
            None if job is None else await read_stage2_result(context.tenant, job_id)
        )
        translations = (
            {} if job is None else await read_translations(context.tenant, job_id)
        )
    except JobStoreUnavailable:
        raise JobStoreUnavailableResponse() from None
    _audit.info(
        "call_job_read",
        extra={
            "event": "call_job_read",
            "tenant": context.tenant,
            "request_id": context.request_id,
            "job_id": job_id,
            "found": job is not None,
            "status": None if job is None else job.status.value,
            "result_held": result is not None,
            "stage2_result_held": stage2 is not None,
        },
    )
    if job is None:
        raise CallJobNotFound()
    return CallJobView(
        job_id=job.job_id,
        status=job.status,
        reason=job.reason,
        delivery=job.delivery,
        stage2=job.stage2,
        result=result,
        stage2_result=stage2,
        translations=translations,
    )
