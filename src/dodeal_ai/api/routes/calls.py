"""Unit B's HTTP surface: push a recorded call (register item 50).

THE SERVICE CHAIN ONLY. The CRM pushes with its own token for the tenant the
Host names; a person's token is 401. Gate 4 is the tenant's CALLS request
counter (`cost:calls:tenant`), never the live one, and the scope names the
call's author as an asserted subject (`author:<author_id>`).

  POST /api/v1/calls/jobs    202 {job_id, status}; the same call again is the
                             same job_id; 403 calls_not_enabled while the
                             tenant's `unit_b.calls_enabled` is off
  POST /api/v1/calls/reanalysis
                             202 {job_id, status}: a stored call's transcript
                             analysed again with the versions in force, on
                             its own counter (`cost:reanalysis:tenant`); the
                             same call, stages and versions is the same job
  POST /api/v1/calls/jobs/{job_id}/translation {target: ar|en}
                             202; 409 result_expired, 409 already_in_language;
                             counted on the READS counter, sent back as
                             call.translation
  POST /api/v1/calls/jobs/{job_id}/whatsapp {language: one of 12 codes}
                             200 {job_id, language, dialect, text}: the
                             suggestion written again in that language, one
                             paid pass per language, held beside the result;
                             409 result_expired, 409 whatsapp_in_progress;
                             counted on the READS counter
  GET  /api/v1/calls/jobs/{job_id}
                             {job_id, status, reason, delivery, stage2,
                             result, stage2_result, translations, whatsapp},
                             each while held;
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
    service_gate4_reanalysis_cost,
)
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.llm import LLMClient, get_llm_client
from dodeal_ai.units.call_intelligence.admission import admit_call
from dodeal_ai.units.call_intelligence.config import resolve_calls_config
from dodeal_ai.units.call_intelligence.reads import CallJobView, read_call_job
from dodeal_ai.units.call_intelligence.reanalysis import admit_reanalysis
from dodeal_ai.units.call_intelligence.schemas import (
    CallJobAccepted,
    CallJobRequest,
    ReanalysisRequest,
)
from dodeal_ai.units.call_intelligence.translation import (
    TranslationAccepted,
    TranslationRequest,
    request_translation,
)
from dodeal_ai.units.call_intelligence.whatsapp import (
    WhatsAppRequest,
    WhatsAppSuggestion,
    regenerate_whatsapp,
)

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


@router.post("/reanalysis", status_code=202)
async def create_reanalysis_job(
    body: ReanalysisRequest,
    context: Annotated[RequestContext, Depends(service_gate4_reanalysis_cost)],
) -> CallJobAccepted:
    """Analyse a stored call again. The transcript is the CRM's copy of stage
    1's; the audio is never fetched and never transcribed again."""
    return await admit_reanalysis(
        context.scope_for_author(body.author_id, budget="calls"),
        body,
        await resolve_calls_config(context.tenant),
        now=datetime.now(UTC),
    )


@router.post("/jobs/{job_id}/translation", status_code=202)
async def create_translation(
    body: TranslationRequest,
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
    job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
) -> TranslationAccepted:
    """Translate a done call's transcript; the answer comes as
    call.translation and through the status route."""
    return await request_translation(context, job_id, body)


@router.post("/jobs/{job_id}/whatsapp")
async def create_whatsapp(
    body: WhatsAppRequest,
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
) -> WhatsAppSuggestion:
    """Write a done call's WhatsApp suggestion again in the language asked;
    the answer comes back here and through the status route."""
    return await regenerate_whatsapp(context, job_id, body, llm, settings)


@router.get("/jobs/{job_id}")
async def read_call_job_route(
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
    job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
) -> CallJobView:
    """Where one call job is. Polling is a read: it never moves the calls
    counter, and it never changes the job."""
    return await read_call_job(context, job_id)
