"""Gate 2: host subdomain must match the token's authoritative subdomain."""

from __future__ import annotations

import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.tenancy import (
    TenantMismatchError,
    check_tenant,
    subdomain_from_host,
)


def _identity(tenant: str = "nasir3") -> Identity:
    return Identity(tenant=tenant, subject="42", database="crm_nasir3")


def test_matching_subdomain_passes():
    assert check_tenant(_identity("nasir3"), "nasir3.dodealcrm.com") == "nasir3"


def test_matching_subdomain_with_port_passes():
    assert check_tenant(_identity("nasir3"), "nasir3.dodealcrm.com:8000") == "nasir3"


def test_mismatched_subdomain_denied():
    with pytest.raises(TenantMismatchError) as exc:
        check_tenant(_identity("nasir3"), "other.dodealcrm.com")
    assert exc.value.reason_code == "tenant_mismatch"


def test_absent_host_denied():
    with pytest.raises(TenantMismatchError):
        check_tenant(_identity("nasir3"), None)


def test_host_without_subdomain_denied():
    with pytest.raises(TenantMismatchError):
        check_tenant(_identity("nasir3"), "dodealcrm.com")


def test_subdomain_parsing():
    assert subdomain_from_host("nasir3.dodealcrm.com") == "nasir3"
    assert subdomain_from_host("nasir3.dodealcrm.com:8000") == "nasir3"
    assert subdomain_from_host("dodealcrm.com") is None
    assert subdomain_from_host("localhost") is None
    assert subdomain_from_host(None) is None
