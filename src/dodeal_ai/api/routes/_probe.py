"""Temporary probe route exercising the gate chain over HTTP. Scaffolding;
remove before the first real feature route.

Runs Gate 1 (auth) and Gate 2 (tenancy). Gate 3 (permissions) is parked pending
a confirmed permission model, so no permission is required here yet.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from dodeal_ai.core.auth.dependencies import gate4_cost
from dodeal_ai.core.context import RequestContext

router = APIRouter(prefix="/_probe", tags=["scaffolding"])


@router.get("/protected")
async def protected(
    context: Annotated[RequestContext, Depends(gate4_cost)],
) -> dict:
    return {
        "tenant": context.tenant,
        "subject": context.subject,
        "database": context.database,
        "request_id": context.request_id,
    }
