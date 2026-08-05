"""RequestContext — the single, immutable source of request identity.

Built once, after the gates have verified the token (Gate 1), confirmed the
tenant (Gate 2), and resolved permissions (Gate 3). Everything downstream —
tools, audit, feature units — reads tenant identity from HERE and nowhere else.
No component re-parses the token or trusts a header; if it isn't on the
RequestContext, it isn't authoritative.

Frozen: once constructed it cannot be mutated. This is a security property, not
a convenience — a request's tenant must not be reassignable mid-flight.
"""
from __future__ import annotations

from dataclasses import dataclass

from dodeal_ai.core.auth.claims import Identity


@dataclass(frozen=True)
class RequestContext:
    tenant: str
    subject: str
    database: str
    roles: tuple[str, ...]
    permissions: frozenset[str]
    request_id: str

    @classmethod
    def from_identity(
        cls,
        identity: Identity,
        *,
        permissions: frozenset[str],
        request_id: str,
    ) -> RequestContext:
        """Assemble the context from a mapped Identity (Gate 1 output) plus the
        permissions resolved by Gate 3 and the middleware-issued request_id.

        roles come straight off the verified Identity; permissions are computed
        separately (Step 6) — kept distinct so 'what the token said' and 'what we
        granted' never blur.
        """
        return cls(
            tenant=identity.tenant,
            subject=identity.subject,
            database=identity.database,
            roles=identity.roles,
            permissions=permissions,
            request_id=request_id,
        )

    def has_permission(self, permission: str) -> bool:
        """Single read-path Gate 3 uses to enforce default-deny."""
        return permission in self.permissions