"""TEMPORARY probe route — SCAFFOLDING, delete before any real feature ships.

Exists only to exercise the gate chain end-to-end over HTTP for the Phase 0
exit demo. It returns nothing sensitive: just proof that the chain ran and the
RequestContext it produced. No feature logic, no tool calls, no data access.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from dodeal_ai.core.auth.dependencies import require_context
from dodeal_ai.core.context import RequestContext

router = APIRouter(prefix="/_probe", tags=["scaffolding"])


@router.get("/protected")
def protected(
    context: Annotated[RequestContext, Depends(require_context("lead:read"))],
) -> dict:
    """Requires a valid token, consistent tenant, and the lead:read permission.
    Echoes back non-sensitive context fields to prove the chain executed."""
    return {
        "tenant_id": context.tenant_id,
        "subject": context.subject,
        "roles": list(context.roles),
        "request_id": context.request_id,
    }