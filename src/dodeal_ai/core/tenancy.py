"""Gate 2 — tenant isolation by subdomain match.

The tenant is the `subdomain` claim on the verified token, and it is
authoritative. Every request also arrives at a tenant-specific host
(`<subdomain>.dodealcrm.com`), from which a subdomain can be read. The two must
match: the token states which tenant the user belongs to, the host states which
tenant is being addressed. A mismatch means a valid token is being used against
another tenant's host, and is denied (403).

Isolation is enforced backend-side by tenant database, keyed on subdomain; lead
records carry no tenant field. The check here is therefore a subdomain match,
not a comparison of a field on returned data.
"""

from __future__ import annotations

from dodeal_ai.core.auth.claims import Identity


class TenantMismatchError(Exception):
    """The host subdomain did not match the token's authoritative subdomain.
    Raised with an audit-safe reason code; surfaced as 403 by the gate."""

    def __init__(self, reason_code: str = "tenant_mismatch"):
        self.reason_code = reason_code
        super().__init__(reason_code)


def subdomain_from_host(host: str | None) -> str | None:
    """Extract the leading subdomain label from a Host header value.

    Returns None if the host is absent or has no subdomain label (fewer than
    three dot-separated parts, e.g. a bare domain or `localhost`). A port suffix
    is ignored.
    """
    if not host:
        return None
    host = host.split(":", 1)[0]  # drop any :port
    labels = host.split(".")
    if len(labels) < 3:
        # No subdomain present (e.g. "dodealcrm.com" or "localhost").
        return None
    return labels[0]


def check_tenant(identity: Identity, host: str | None) -> str:
    """Confirm the host subdomain matches the token's authoritative subdomain.

    Returns the authoritative tenant subdomain on success. Raises
    TenantMismatchError if the host subdomain is absent or does not match.
    """
    authoritative = identity.tenant
    host_subdomain = subdomain_from_host(host)
    if host_subdomain is None or host_subdomain != authoritative:
        raise TenantMismatchError()
    return authoritative
