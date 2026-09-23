"""Probe route exercising the gate chain over HTTP. Test scaffolding only.

create_app() does not mount it (register item 93), so no deployment serves it.
The gate assertions in test_chain / test_exit_demo / test_audit mount it on
their own app through tests/helpers/probe_app.py.

`/_probe/protected` runs the user chain -- Gate 1 (auth), Gate 2 (tenancy) and
Gate 4 (cost) -- and `/_probe/service` the CRM's service chain (register item
D1). Gate 3 (permissions) is parked pending a confirmed permission model, so no
permission is required on either.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from dodeal_ai.core.auth.dependencies import gate4_cost, service_gate4_cost
from dodeal_ai.core.context import RequestContext

router = APIRouter(prefix="/_probe", tags=["scaffolding"])


@router.get("/service")
async def service(
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
) -> dict:
    """The service chain (register item D1), for the tests that assert it."""
    return {
        "tenant": context.tenant,
        "subject": context.subject,
        "principal": context.principal,
        "request_id": context.request_id,
    }


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
