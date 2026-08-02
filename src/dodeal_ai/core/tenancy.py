"""Gate 2 — tenant isolation check.

The tenant on the verified token (Gate 1's Identity) is AUTHORITATIVE. A tenant
arriving any other way — a header, a subdomain, a body field — is only a
cross-check: if present it must match the token, and a mismatch is a hard deny
(403). If absent, the token stands alone; we do not fall back to the header for
identity, ever.

This is the gate the exit demo leans on: tenant B's token reaching for tenant
A's data must be blocked here. The check is a plain function so it can be tested
with no HTTP in play.
"""
from __future__ import annotations

from dodeal_ai.core.auth.claims import Identity


class TenantMismatchError(Exception):
    """A cross-check tenant contradicted the token's authoritative tenant.
    Turned into a 403 by the dependency layer; reason code is audit-safe."""

    def __init__(self, reason_code: str = "tenant_mismatch"):
        self.reason_code = reason_code
        super().__init__(reason_code)


def check_tenant(identity: Identity, header_tenant: str | None) -> str:
    """Confirm the request's tenant. Returns the authoritative tenant_id (from
    the token) on success; raises TenantMismatchError on a cross-check conflict.

    - header_tenant is None            -> token is authoritative, pass.
    - header_tenant == token tenant    -> consistent, pass.
    - header_tenant != token tenant    -> deny (403).

    The header is NEVER used as the identity source — only as a tripwire that
    catches a caller trying to act across tenants.
    """
    authoritative = identity.tenant_id
    if header_tenant is not None and header_tenant != authoritative:
        raise TenantMismatchError()
    return authoritative