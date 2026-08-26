"""Gate 3: role->permission resolution and default-deny enforcement.

Gate 3 is currently parked out of the live chain (the token carries no roles and
the permission model is undecided). These unit tests exercise the resolution and
enforcement logic directly, so it stays proven and ready to wire when the model
is confirmed.

That includes the FastAPI adapter, require_context(): parked code that is only
exercised the day it is wired is code nobody has run. It is called here with a
stub Request and a hand-built RequestContext -- no app, no route, no gate chain
-- so the 403 shape and the audit line are pinned now rather than discovered
during the wiring change.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.auth.dependencies import require_context
from dodeal_ai.core.authz import permissions
from dodeal_ai.core.authz.permissions import (
    PermissionDeniedError,
    require_permission,
    resolve_permissions,
)
from dodeal_ai.core.context import RequestContext


def _identity(roles, tenant="tenant-a"):
    return Identity(
        tenant=tenant, subject="42", database="crm_tenant_a", roles=tuple(roles)
    )


def _context(roles, request_id="req-1"):
    ident = _identity(roles)
    perms = resolve_permissions(ident)
    return RequestContext.from_identity(ident, permissions=perms, request_id=request_id)


def test_agent_resolves_expected_permissions():
    perms = resolve_permissions(_identity(("agent",)))
    assert perms == frozenset({"lead:read", "note:read"})


def test_no_role_grants_a_write_permission():
    # The service has no write path to the CRM (ASSUMPTIONS 3.1). If a
    # ":write" grant ever reappears in the placeholder table without a write
    # endpoint behind it, this fails rather than quietly widening the table.
    for role in ("agent", "viewer"):
        granted = resolve_permissions(_identity((role,)))
        assert not [p for p in granted if p.endswith(":write")], role


def test_permission_outside_the_table_is_denied():
    # A permission no role holds -- default deny, with nothing implicit and no
    # wildcard, for a granted role as much as an unknown one.
    ctx = _context(("agent",))
    require_permission(ctx, "note:read")  # granted -> no raise
    with pytest.raises(PermissionDeniedError):
        require_permission(ctx, "note:write")


def test_agent_can_read_notes():
    ctx = _context(("agent",))
    require_permission(ctx, "note:read")  # no raise


def test_no_roles_is_default_deny():
    ctx = _context(())
    with pytest.raises(PermissionDeniedError) as exc:
        require_permission(ctx, "lead:read")
    assert exc.value.reason_code == "permission_denied"


def test_unknown_role_grants_nothing():
    ctx = _context(("superadmin",))
    assert ctx.permissions == frozenset()
    with pytest.raises(PermissionDeniedError):
        require_permission(ctx, "lead:read")


def test_multiple_roles_union_permissions(monkeypatch):
    # Against a STUB table, because the two placeholder roles happen to grant
    # the same set today -- asserting the union over the real table would pass
    # without the union loop running at all. The table is a placeholder
    # (pending Product); the union rule is not.
    monkeypatch.setattr(
        permissions,
        "_ROLE_PERMISSIONS",
        {
            "reader": frozenset({"lead:read"}),
            "noter": frozenset({"note:read"}),
        },
    )
    assert resolve_permissions(_identity(("reader", "noter"))) == frozenset(
        {"lead:read", "note:read"}
    )


# --- require_context(): the parked FastAPI adapter ---------------------------


def _stub_request() -> Request:
    """The minimum ASGI scope Starlette needs to build a Request.
    require_context's dependency never reads it -- it takes request_id and
    tenant from the RequestContext -- but it is in the signature."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
    )


def _bare_context(permissions=frozenset()) -> RequestContext:
    return RequestContext(
        tenant="tenant-a",
        subject="42",
        database="crm_tenant_a",
        roles=(),
        permissions=permissions,
        request_id="req-parked-1",
    )


def test_require_context_denies_with_403_and_audits_the_reason(json_log):
    dependency = require_context("lead:read")

    with pytest.raises(HTTPException) as raised:
        dependency(_stub_request(), _bare_context())

    assert raised.value.status_code == 403
    assert raised.value.detail == "Forbidden"
    # The specific reason goes to the audit log only, never to the client.
    assert "permission_denied" not in str(raised.value.detail)

    deny_lines = [
        line
        for line in json_log()
        if line["logger"] == "dodeal_ai.audit" and line["decision"] == "deny"
    ]
    assert any(
        line["gate"] == "authz"
        and line["reason_code"] == "permission_denied"
        and line["request_id"] == "req-parked-1"
        and line["tenant"] == "tenant-a"
        for line in deny_lines
    )


def test_require_context_allows_and_audits_when_the_permission_is_held(json_log):
    dependency = require_context("lead:read")
    context = _bare_context(permissions=frozenset({"lead:read"}))

    assert dependency(_stub_request(), context) is context

    allow_lines = [
        line
        for line in json_log()
        if line["logger"] == "dodeal_ai.audit" and line["decision"] == "allow"
    ]
    assert any(
        line["gate"] == "authz"
        and line["reason_code"] == "ok"
        and line["request_id"] == "req-parked-1"
        for line in allow_lines
    )
