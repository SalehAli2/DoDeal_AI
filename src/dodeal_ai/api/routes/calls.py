"""Unit B's HTTP surface: push a recorded call (register item 50).

THE SERVICE CHAIN ONLY. The CRM pushes with its own token for the tenant the
Host names; a person's token is 401. Gate 4 is the tenant's CALLS request
counter (`cost:calls:tenant`), never the live one, and the scope names the
call's author as an asserted subject (`author:<author_id>`).

  POST /api/v1/calls/jobs    202 {job_id, status}; the same call again is the
                             same job_id; 403 calls_not_enabled while the
                             tenant's `unit_b.calls_enabled` is off
  GET  /api/v1/calls/jobs/{job_id}
                             {job_id, status, reason, delivery, stage2,
                             result, stage2_result}, each result while held;
                             another tenant's job is 404. Counted on the
                             READS counter, and every read is audited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from dodeal_ai.core.auth.dependencies import (
    service_gate4_calls_cost,
    service_gate4_reads_cost,
)
from dodeal_ai.core.context import RequestContext
from dodeal_ai.units.call_intelligence.admission import admit_call
from dodeal_ai.units.call_intelligence.config import resolve_calls_config
from dodeal_ai.units.call_intelligence.reads import CallJobView, read_call_job
from dodeal_ai.units.call_intelligence.schemas import CallJobAccepted, CallJobRequest

router = APIRouter(prefix="/api/v1/calls", tags=["unit-b"])


@router.post("/jobs", status_code=202)
async def create_call_job(
    body: CallJobRequest,
    context: Annotated[RequestContext, Depends(service_gate4_calls_cost)],
) -> CallJobAccepted:
    """Admit one recorded call. Work happens on a worker, never here: the
    answer is the job to poll and the callbacks to expect."""
    return await admit_call(
        context.scope_for_author(body.author_id, budget="calls"),
        body,
        await resolve_calls_config(context.tenant),
        now=datetime.now(UTC),
    )


@router.get("/jobs/{job_id}")
async def read_call_job_route(
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
    job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
) -> CallJobView:
    """Where one call job is. Polling is a read: it never moves the calls
    counter, and it never changes the job."""
    return await read_call_job(context, job_id)
