"""Gate 3: role->permission resolution and default-deny enforcement.

Gate 3 is currently parked out of the live chain (the token carries no roles and
the permission model is undecided). These unit tests exercise the resolution and
enforcement logic directly, so it stays proven and ready to wire when the model
is confirmed.
"""
from __future__ import annotations

import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.authz.permissions import (
    PermissionDeniedError,
    require_permission,
    resolve_permissions,
)
from dodeal_ai.core.context import RequestContext


def _identity(roles, tenant="nasir3"):
    return Identity(
        tenant=tenant, subject="42", database="crm_nasir3", roles=tuple(roles)
    )


def _context(roles, request_id="req-1"):
    ident = _identity(roles)
    perms = resolve_permissions(ident)
    return RequestContext.from_identity(
        ident, permissions=perms, request_id=request_id
    )


def test_agent_resolves_expected_permissions():
    perms = resolve_permissions(_identity(("agent",)))
    assert perms == frozenset({"lead:read", "note:read", "note:write"})


def test_viewer_cannot_write():
    ctx = _context(("viewer",))
    require_permission(ctx, "note:read")  # granted -> no raise
    with pytest.raises(PermissionDeniedError):
        require_permission(ctx, "note:write")


def test_agent_can_write():
    ctx = _context(("agent",))
    require_permission(ctx, "note:write")  # no raise


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


def test_multiple_roles_union_permissions():
    perms = resolve_permissions(_identity(("viewer", "agent")))
    assert perms == frozenset({"lead:read", "note:read", "note:write"})