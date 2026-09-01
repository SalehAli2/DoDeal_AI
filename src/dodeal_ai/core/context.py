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
class TenantScope:
    """The narrow slice of request identity the TOOL LAYER is allowed to see:
    whose data, on whose behalf, against which database, under which request id.

    Design note 0001, Decision 1: the tool layer takes a TenantScope, NOT a
    RequestContext. A RequestContext carries roles and permissions -- an
    authorization answer for a *user* request -- and a tool has no business
    reading them. Passing the whole context also means a tool cannot be called
    from anywhere that lacks one, which is exactly the problem when the
    principal is a service token or a signed job payload rather than a user's
    JWT: those have a tenant and a subject but no roles to speak of.

    So this is the seam D1 named. When a second principal source appears, it
    produces a TenantScope and every tool call site keeps working unchanged.

    Constructed in ONE place -- RequestContext.scope() below. A test greps src/
    for other construction sites, because a scope built from anything other
    than a verified context is a scope whose tenant nobody checked.
    """

    tenant: str
    subject: str
    database: str
    request_id: str


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

    def scope(self) -> TenantScope:
        """Narrow this context to what the tool layer may see.

        Deliberately NOT a property: it is a boundary crossing, and reading
        like one at the call site (`client.get_lead(scope, ...)` built from
        `context.scope()`) is the point. Nothing is re-derived here -- the
        tenant is the one the gates already verified.
        """
        return TenantScope(
            tenant=self.tenant,
            subject=self.subject,
            database=self.database,
            request_id=self.request_id,
        )
