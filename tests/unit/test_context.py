"""RequestContext: immutability and faithful construction from an Identity."""

from __future__ import annotations

import dataclasses

import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.context import RequestContext


def _ctx(**overrides) -> RequestContext:
    base = {
        "tenant": "nasir3",
        "subject": "42",
        "database": "crm_nasir3",
        "roles": ("agent",),
        "permissions": frozenset({"lead:read"}),
        "request_id": "req-123",
    }
    base.update(overrides)
    return RequestContext(**base)


def test_from_identity_carries_all_fields():
    ident = Identity(
        tenant="nasir3", subject="42", database="crm_nasir3", roles=("agent",)
    )
    ctx = RequestContext.from_identity(
        ident, permissions=frozenset({"lead:read"}), request_id="req-123"
    )
    assert ctx.tenant == "nasir3"
    assert ctx.subject == "42"
    assert ctx.database == "crm_nasir3"
    assert ctx.roles == ("agent",)
    assert ctx.permissions == frozenset({"lead:read"})
    assert ctx.request_id == "req-123"


def test_context_is_frozen():
    ctx = _ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.tenant_id = "tenant-b"


def test_has_permission_is_default_deny():
    ctx = _ctx(permissions=frozenset({"lead:read"}))
    assert ctx.has_permission("lead:read") is True
    assert ctx.has_permission("lead:write") is False


def test_roles_and_permissions_are_distinct():
    # A role the token asserted does not itself grant a permission; permissions
    # are what Gate 3 resolved. Keep them separate on the context.
    ctx = _ctx(roles=("agent",), permissions=frozenset())
    assert ctx.roles == ("agent",)
    assert ctx.has_permission("agent") is False
