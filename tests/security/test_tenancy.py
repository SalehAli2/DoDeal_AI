"""Gate 2: token tenant is authoritative; header is a cross-check only."""
from __future__ import annotations

import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.tenancy import TenantMismatchError, check_tenant


def _identity(tenant_id: str = "tenant-a") -> Identity:
    return Identity(tenant_id=tenant_id, subject="user-1", roles=("agent",))


def test_no_header_passes_with_token_tenant():
    assert check_tenant(_identity("tenant-a"), None) == "tenant-a"


def test_matching_header_passes():
    assert check_tenant(_identity("tenant-a"), "tenant-a") == "tenant-a"


def test_mismatched_header_denied():
    # The cross-tenant tripwire: token says A, header says B -> 403.
    with pytest.raises(TenantMismatchError) as exc:
        check_tenant(_identity("tenant-a"), "tenant-b")
    assert exc.value.reason_code == "tenant_mismatch"


def test_header_is_never_the_identity_source():
    # Even a present, different header cannot promote itself to authoritative;
    # the returned tenant is always the token's, never the header's.
    with pytest.raises(TenantMismatchError):
        check_tenant(_identity("tenant-a"), "tenant-b")
    # And when they agree, the value returned is the token's tenant.
    assert check_tenant(_identity("tenant-a"), "tenant-a") == "tenant-a"