"""Gate 2: the Host header must resolve, as a whole, to the token's
authoritative tenant -- not just its first label (audit finding H4)."""

from __future__ import annotations

import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.tenancy import TenantMismatchError, check_tenant

_BASE_DOMAIN = "dodealcrm.com"


def _identity(tenant: str = "tenant-a") -> Identity:
    return Identity(tenant=tenant, subject="42", database="crm_tenant_a")


ALLOW = None  # sentinel: no exception expected

CASES = [
    ("tenant-a.dodealcrm.com", ALLOW),
    ("Tenant-A.dodealcrm.com", ALLOW),  # case-insensitive (was 403)
    ("TENANT-A.DODEALCRM.COM", ALLOW),
    ("tenant-a.dodealcrm.com.", ALLOW),  # trailing dot
    ("tenant-a.dodealcrm.com:443", ALLOW),
    ("tenant-a.evil.com", "invalid_host"),  # was ALLOW -- the H4 bypass
    ("a.tenant-a.dodealcrm.com", "invalid_host"),
    ("dodealcrm.com", "invalid_host"),
    ("localhost", "invalid_host"),
    ("[::1]:8000", "invalid_host"),
    (None, "invalid_host"),
    ("tenant-b.dodealcrm.com", "tenant_mismatch"),
]


@pytest.mark.parametrize("host,expected", CASES)
def test_check_tenant(host, expected):
    if expected is ALLOW:
        assert check_tenant(_identity("tenant-a"), host, _BASE_DOMAIN) == "tenant-a"
        return
    with pytest.raises(TenantMismatchError) as exc:
        check_tenant(_identity("tenant-a"), host, _BASE_DOMAIN)
    assert exc.value.reason_code == expected
