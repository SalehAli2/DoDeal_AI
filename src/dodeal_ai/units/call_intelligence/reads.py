"""Reading a call job back (register item 50): status, reason, and the result
while it is held.

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
from dodeal_ai.core.jobs import JobStatus, JobStoreUnavailable, read_job, read_result

_audit = logging.getLogger("dodeal_ai.audit")


class CallJobView(BaseModel):
    """What the CRM reads: where the job is, why, and its result while held."""

    job_id: str
    status: JobStatus
    reason: str | None
    result: dict[str, object] | None


async def read_call_job(context: RequestContext, job_id: str) -> CallJobView:
    """This tenant's job, or 404; the read is audited either way."""
    try:
        job = await read_job(context.tenant, job_id)
        result = None if job is None else await read_result(context.tenant, job_id)
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
        },
    )
    if job is None:
        raise CallJobNotFound()
    return CallJobView(
        job_id=job.job_id, status=job.status, reason=job.reason, result=result
    )
