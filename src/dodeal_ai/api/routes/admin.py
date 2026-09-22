"""The tenant administrator's rules, changed at runtime (register item 97).

Three routes behind the SERVICE chain -- the CRM's admin screen calls them
with its own token for the tenant the Host names:

  PUT  /api/v1/admin/tenant-config          a `unit_a` section, validated by the
                                            SAME parser as the tenant file, stored
                                            as a new dated version
  GET  /api/v1/admin/tenant-config          the rules in force, with their
                                            version, set_at and source
  GET  /api/v1/admin/tenant-config/history  past versions, newest first, at
                                            most 50

A refused section is 422 invalid_tenant_config with no field name and no value:
the parser's own message could quote what was sent. The one log line is
tenant_config_changed, with the tenant and the version and nothing else.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from dodeal_ai.core.auth.dependencies import service_gate4_cost
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import (
    InvalidTenantConfig,
    TenantConfigConflict,
    TenantConfigUnavailable,
)
from dodeal_ai.core.tenant_config import (
    InvalidOverride,
    OverrideConflict,
    OverrideUnavailable,
    override_history,
    resolve_section,
    set_override,
)
from dodeal_ai.units.structured_intelligence.config import (
    UNIT_A_SECTION,
    config_of,
    section_of,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.put("/tenant-config")
async def put_tenant_config(
    request: Request,
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
) -> dict[str, str]:
    """Store a new `unit_a` section as the rules in force for this tenant.

    The body is read raw so that anything the parser refuses -- not JSON, not
    an object, a bad field -- is the same 422 invalid_tenant_config. Its
    `config_version` is set by the service to the new dated version.
    """
    try:
        body = json.loads(await request.body())
    except ValueError:
        raise InvalidTenantConfig() from None
    try:
        record = await set_override(
            context.tenant, UNIT_A_SECTION, body, now=datetime.now(UTC)
        )
    except InvalidOverride:
        raise InvalidTenantConfig() from None
    except OverrideUnavailable:
        raise TenantConfigUnavailable() from None
    except OverrideConflict:
        raise TenantConfigConflict() from None
    return {"version": record.version, "set_at": record.set_at}


@router.get("/tenant-config")
async def read_tenant_config(
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
) -> dict[str, object]:
    """The rules a judgement made now would use, resolved exactly as a
    judgement resolves them, and where they came from."""
    resolved = await resolve_section(context.tenant, UNIT_A_SECTION)
    config = config_of(resolved)
    return {
        "version": config.config_version,
        "set_at": resolved.set_at,
        "source": resolved.source,
        UNIT_A_SECTION: section_of(config),
    }


@router.get("/tenant-config/history")
async def read_tenant_config_history(
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
) -> dict[str, object]:
    """Past runtime versions, newest first, at most 50."""
    try:
        records = await override_history(context.tenant)
    except OverrideUnavailable:
        raise TenantConfigUnavailable() from None
    return {
        "versions": [
            {
                "version": record.version,
                "set_at": record.set_at,
                UNIT_A_SECTION: record.sections.get(UNIT_A_SECTION),
            }
            for record in records
        ]
    }
