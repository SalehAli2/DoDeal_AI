"""The tenant administrator's rules, changed at runtime (register item 97).

Three routes behind the SERVICE chain -- the CRM's admin screen calls them
with its own token for the tenant the Host names -- counted on the tenant's
READS request counter, never the live one (register item 153):

  PUT  /api/v1/admin/tenant-config          a WHOLE `unit_a` section, validated by
                                            the SAME parser as the tenant file,
                                            stored with a new policy_version
  GET  /api/v1/admin/tenant-config          the rules in force, with their
                                            version, policy_version, set_at and
                                            source
  GET  /api/v1/admin/tenant-config/history  past versions, newest first, at
                                            most 50, each with both stamps
  PUT  /api/v1/admin/tenant-config/unit_b   a WHOLE `unit_b` section (register
  GET  /api/v1/admin/tenant-config/unit_b   item 50), and the call rules in force

Each PUT MERGES its section into the stored record (register item 193): a
`unit_b` PUT never drops `unit_a` or moves its stamps, and the reverse.

PUT REPLACES THE WHOLE SECTION; it never merges. A field the body leaves out
goes back to the default, not to what was in force -- so a caller reads the
section with GET first, changes what it means to, and PUTs all of it back.

TWO STAMPS (register item 97). `version` is the config_version judgements are
stamped with, and it moves only when a mark-affecting field changes
(config.MARK_AFFECTING_FIELDS); `policy_version`,
tenant-policy-<tenant>-<YYYYMMDD>-<n>, moves on every accepted PUT.

A refused section is 422 invalid_tenant_config with no field name and no value:
the parser's own message could quote what was sent. The one log line is
tenant_config_changed, with the tenant and both stamps and nothing else.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from dodeal_ai.core.auth.dependencies import service_gate4_reads_cost
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
    VersionKeeper,
    override_history,
    resolve_section,
    set_override,
)
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    calls_config_of,
    calls_section_of,
    new_config_version,
)
from dodeal_ai.units.structured_intelligence.config import (
    UNIT_A_SECTION,
    config_of,
    kept_config_version,
    section_of,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.put("/tenant-config")
async def put_tenant_config(
    request: Request,
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
) -> dict[str, str | None]:
    """Store a new `unit_a` section as the rules in force for this tenant.

    The body REPLACES the whole section: a field it leaves out is the default,
    not the value in force, so read the section with GET first and PUT all of
    it back. It is read raw so that anything the parser refuses -- not JSON,
    not an object, a bad field -- is the same 422 invalid_tenant_config.

    Both stamps are the service's, whatever the body says: `policy_version` is
    the next dated one, and `config_version` stays the one in force unless a
    mark-affecting field changed, when it too is the next dated one.
    """
    return await _put_section(request, context, UNIT_A_SECTION, kept_config_version)


async def _put_section(
    request: Request,
    context: RequestContext,
    section: str,
    keep_version: VersionKeeper,
) -> dict[str, str | None]:
    """One PUT, for either section: read raw, validate with the section's
    parser, merge it into the stored record, and answer with its stamps."""
    try:
        body = json.loads(await request.body())
    except ValueError:
        raise InvalidTenantConfig() from None
    try:
        record = await set_override(
            context.tenant,
            section,
            body,
            now=datetime.now(UTC),
            keep_version=keep_version,
        )
    except InvalidOverride:
        raise InvalidTenantConfig() from None
    except OverrideUnavailable:
        raise TenantConfigUnavailable() from None
    except OverrideConflict:
        raise TenantConfigConflict() from None
    return {
        "version": record.version,
        "policy_version": record.policy_version,
        "set_at": record.set_at,
    }


@router.get("/tenant-config")
async def read_tenant_config(
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
) -> dict[str, object]:
    """The rules a judgement made now would use, resolved exactly as a
    judgement resolves them, and where they came from."""
    resolved = await resolve_section(context.tenant, UNIT_A_SECTION)
    config = config_of(resolved)
    return {
        "version": config.config_version,
        "policy_version": config.policy_version,
        "set_at": resolved.set_at,
        "source": resolved.source,
        UNIT_A_SECTION: section_of(config),
    }


@router.get("/tenant-config/history")
async def read_tenant_config_history(
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
) -> dict[str, object]:
    """Past runtime versions, newest first, at most 50, each with its
    config_version (`version`) and its policy_version (null on a record stored
    before the second stamp existed)."""
    try:
        records = await override_history(context.tenant)
    except OverrideUnavailable:
        raise TenantConfigUnavailable() from None
    return {
        "versions": [
            {
                "version": record.version,
                "policy_version": record.policy_version,
                "set_at": record.set_at,
                UNIT_A_SECTION: record.sections.get(UNIT_A_SECTION),
                UNIT_B_SECTION: record.sections.get(UNIT_B_SECTION),
            }
            for record in records
        ]
    }


@router.put("/tenant-config/unit_b")
async def put_calls_config(
    request: Request,
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
) -> dict[str, str | None]:
    """Store a new `unit_b` section as the call rules in force (register item
    50). The body REPLACES the whole section, as for `unit_a`: a field left out
    is the default, and every switch defaults off. `unit_a` is untouched.

    Every accepted PUT is a new dated config_version and policy_version. A
    refused section -- an http callback, calls on with no audio host -- is 422
    invalid_tenant_config, naming no field.
    """
    return await _put_section(request, context, UNIT_B_SECTION, new_config_version)


@router.get("/tenant-config/unit_b")
async def read_calls_config(
    context: Annotated[RequestContext, Depends(service_gate4_reads_cost)],
) -> dict[str, object]:
    """The call rules a job admitted now would use, and where they came from."""
    resolved = await resolve_section(context.tenant, UNIT_B_SECTION)
    return {
        "version": resolved.version,
        "policy_version": resolved.policy_version,
        "set_at": resolved.set_at,
        "source": resolved.source,
        UNIT_B_SECTION: calls_section_of(calls_config_of(resolved)),
    }
