"""Unit A's HTTP surface: judge a note, resubmit a note, read the versions.

Every route depends on gate4_cost, which is the WHOLE live chain -- Gate 1
(auth) -> Gate 2 (tenancy) -> Gate 4 (cost). Gate 3 is parked; see
core/auth/dependencies.py. One Depends, not a stack: the gates chain through
nested Depends, so listing them individually here would be a second, silently
divergent copy of the order.

The route hands the pipeline a TenantScope, never the RequestContext (design
note 0001, D1). Nothing below re-reads a tenant from anywhere.

NOTE TEXT IS NEVER ACCEPTED IN A BODY. JudgementRequest is two integers and
extra="forbid": the note is already saved in the CRM and is fetched by id
(Design A). A caller posting `note` gets 422 invalid_request -- and, because
core/errors.py replaces FastAPI's stock validation handler, that 422 does not
quote their text back at them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from dodeal_ai.core.auth.dependencies import gate4_cost
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.llm import LLMClient, get_llm_client
from dodeal_ai.tools.leads import LeadsClient, get_leads_client
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import (
    PROMPT_SET_VERSION,
    RUBRIC_VERSION,
    JudgementDeps,
    judge_note,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    Judgement,
    JudgementRequest,
    Versions,
)

router = APIRouter(prefix="/api/v1", tags=["unit-a"])


def _deps(context: RequestContext, leads: LeadsClient, llm: LLMClient) -> JudgementDeps:
    return JudgementDeps(leads=leads, llm=llm, config=get_tenant_config(context.tenant))


@router.post("/notes/judgements")
async def create_judgement(
    request: JudgementRequest,
    context: Annotated[RequestContext, Depends(gate4_cost)],
    leads: Annotated[LeadsClient, Depends(get_leads_client)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
) -> Judgement:
    """Judge one already-saved note."""
    return await judge_note(
        context.scope(),
        request,
        resubmission=False,
        deps=_deps(context, leads, llm),
    )


@router.post("/notes/judgements/resubmission")
async def create_resubmission_judgement(
    request: JudgementRequest,
    context: Annotated[RequestContext, Depends(gate4_cost)],
    leads: Annotated[LeadsClient, Depends(get_leads_client)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
) -> Judgement:
    """Re-judge a note the salesperson has edited after a clarification prompt.

    Same body, same response shape. The difference is entirely in what happens
    to the clarification prompt: none is ever sent, and attempts are read but
    never incremented -- we asked once, they answered, and asking again about
    their answer is how a helpful prompt becomes nagging.

    An edited note has a new fingerprint, so it is a NEW judgement rather than
    a 409: the idempotency key is per note text, not per note id.
    """
    return await judge_note(
        context.scope(),
        request,
        resubmission=True,
        deps=_deps(context, leads, llm),
    )


@router.get("/meta/versions")
async def read_versions(
    context: Annotated[RequestContext, Depends(gate4_cost)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Versions:
    """The four version strings a judgement is stamped with.

    CAREFUL, one field means something slightly different here than on a
    judgement. On a judgement, `model_version` is what the provider REPORTED it
    ran. Here there is no call and so no report, so this is the CONFIGURED pin
    (DODEAL_LLM_MODEL) -- what WOULD run. It is "" until a model is configured,
    which is honest: no model is pinned yet.

    Behind the gates like the other two, so version strings are not a public
    fingerprint of the deployment.
    """
    return Versions(
        rubric_version=RUBRIC_VERSION,
        prompt_version=PROMPT_SET_VERSION,
        model_version=settings.llm_model,
        config_version=get_tenant_config(context.tenant).config_version,
    )
