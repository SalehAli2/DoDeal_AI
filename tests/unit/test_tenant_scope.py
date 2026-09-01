"""TenantScope: built in exactly one place in src/, carries only what the tool
layer may see, and is frozen."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from dodeal_ai.core.context import RequestContext, TenantScope

_SRC = Path(__file__).resolve().parents[2] / "src" / "dodeal_ai"

# The ONE file allowed to construct a scope. A scope built anywhere else in
# src/ is a scope whose tenant no gate verified.
_ALLOWED = {"core/context.py"}

_CONSTRUCTION = re.compile(r"\bTenantScope\(")


def _context(tenant: str = "tenant-a") -> RequestContext:
    return RequestContext(
        tenant=tenant,
        subject="42",
        database="crm_tenant_a",
        roles=("agent",),
        permissions=frozenset({"lead:read"}),
        request_id="req-1",
    )


def test_tenant_scope_is_constructed_in_exactly_one_place_in_src() -> None:
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if relative in _ALLOWED:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _CONSTRUCTION.search(line):
                offenders.append(f"{relative}:{number}")

    assert offenders == [], (
        "TenantScope may only be constructed in core/context.py "
        f"(via RequestContext.scope()); found: {offenders}"
    )


def test_the_allowed_file_really_does_construct_one() -> None:
    # Guards the guard: if scope() were renamed away, the test above would pass
    # vacuously and stop enforcing anything.
    source = (_SRC / "core" / "context.py").read_text(encoding="utf-8")
    assert _CONSTRUCTION.search(source)


def test_scope_carries_the_four_identity_fields() -> None:
    scope = _context().scope()
    assert scope == TenantScope(
        tenant="tenant-a",
        subject="42",
        database="crm_tenant_a",
        request_id="req-1",
    )


def test_scope_drops_roles_and_permissions() -> None:
    # A tool needs to know WHOSE data it is fetching, never what the caller is
    # allowed to do. Dropping them is the point of the type.
    fields = {f.name for f in dataclasses.fields(TenantScope)}
    assert "roles" not in fields
    assert "permissions" not in fields
    assert fields == {"tenant", "subject", "database", "request_id"}


def test_scope_is_frozen() -> None:
    scope = _context().scope()
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.tenant = "tenant-b"  # type: ignore[misc]


def test_scope_does_not_re_derive_the_tenant() -> None:
    # Whatever the gates verified is what the tool layer gets, unchanged.
    assert _context("tenant-b").scope().tenant == "tenant-b"


def test_scope_is_a_method_not_a_property() -> None:
    # Reading like a boundary crossing at the call site is deliberate.
    assert callable(RequestContext.scope)
