"""Gate 2 — tenant isolation by full-host match.

The tenant is the `subdomain` claim on the verified token, and it is
authoritative; extract_identity has already validated and lowercased it as a
single DNS label (core/auth/claims.py::TENANT_LABEL_RE). Every request also
arrives at a tenant-specific host, `<subdomain>.<inbound_base_domain>`, and the
two must match exactly: the token states which tenant the user belongs to, the
Host header states which tenant is being addressed.

The whole host is checked, not just its first label: comparing only the first
label lets a Host like "<tenant>.evil.com" through, because nothing ever looks
past the first dot. Two distinct denial reasons:
  - "invalid_host"    the Host header does not have the required
                       "<label>.<inbound_base_domain>" shape at all (wrong
                       domain, no subdomain, more than one subdomain level, an
                       IPv6 literal, or absent).
  - "tenant_mismatch"  the Host has that shape, but its label is a different
                       (also valid) tenant than the token's.

Isolation is enforced backend-side by tenant database, keyed on subdomain; lead
records carry no tenant field. The check here is therefore a host match, not a
comparison of a field on returned data.

SEAM: if the CRM proxies to us and rewrites Host, the trusted forwarded host is
read HERE, from ingress IPs only — not before the backend confirms the host of
arrival. X-Forwarded-Host is deliberately NOT read by this fix.
"""

from __future__ import annotations

from dodeal_ai.core.auth.claims import Identity, normalise_tenant_label


class TenantMismatchError(Exception):
    """The host did not resolve to the token's authoritative tenant. Raised
    with an audit-safe reason code; surfaced as 403 by the gate. Never carries
    the host or claim value -- only the fixed reason code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def tenant_from_host(host: str | None, base_domain: str) -> str | None:
    """Extract and validate the tenant label from a Host header value.

    Requires the host to equal exactly "<label>.<base_domain>" (case
    insensitive, one trailing dot and one ":<port>" tolerated), with <label>
    containing no dots of its own. Returns the normalised (lowercased,
    DNS-label-validated) tenant, or None if the host does not have that exact
    shape -- including a bare base domain, an extra subdomain level, a
    different domain entirely, an IPv6 literal, or an absent/empty host.
    """
    if not host:
        return None
    if host.startswith("["):
        # IPv6 literal (e.g. "[::1]:8000") -- never a valid tenant host.
        return None

    host = host.lower()

    # Port first, then the root dot: a Host may carry both, and the port always
    # comes last ("tenant-a.dodealcrm.com.:443"). Strip ":port" only when what
    # remains has no further ":" -- an unbracketed IPv6 literal would otherwise
    # have its last segment mistaken for a port. (Bracketed form handled above.)
    if ":" in host:
        head, _, tail = host.rpartition(":")
        if tail.isdigit() and ":" not in head:
            host = head
    host = host.removesuffix(".")

    base_domain = base_domain.lower()
    suffix = "." + base_domain
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label or "." in label:
        return None

    return normalise_tenant_label(label)


def check_tenant(identity: Identity, host: str | None, base_domain: str) -> str:
    """Confirm the Host resolves to the token's authoritative tenant.

    Returns the authoritative tenant on success. Raises TenantMismatchError
    ("invalid_host") if the host does not have the required
    "<label>.<base_domain>" shape at all, or ("tenant_mismatch") if it does but
    names a different tenant. identity.tenant is already a normalised label
    (extract_identity), so a plain equality against the normalised host label
    is correct.
    """
    host_tenant = tenant_from_host(host, base_domain)
    if host_tenant is None:
        raise TenantMismatchError("invalid_host")
    if host_tenant != identity.tenant:
        raise TenantMismatchError("tenant_mismatch")
    return identity.tenant
