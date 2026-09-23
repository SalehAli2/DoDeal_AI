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
from typing import Literal

from dodeal_ai.core.auth.claims import Identity

# Who presented the token: a person (the user JWT) or the CRM itself (the
# service token, core/auth/service.py, register item D1).
type Principal = Literal["user", "service"]

# Which token budget a judgement is charged to (core/cost/limiter.py): the live
# one every judgement shares, the tenant's separate history budget (item 127),
# or its call budget (register item 105), which a worker pauses on.
type TokenBudget = Literal["live", "history", "calls"]


class PrincipalMismatchError(Exception):
    """A scope was asked for that this principal may not produce. A bug, never
    a caller's fault, so it is a 500 and not a refusal. Fixed text only."""

    def __init__(self) -> None:
        super().__init__("principal_mismatch")


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

    Constructed in ONE place -- RequestContext._scope() below. A test greps src/
    for other construction sites, because a scope built from anything other
    than a verified context is a scope whose tenant nobody checked.

    The last three fields default, so a script that builds a scope directly
    keeps working: `principal` says which token the gates verified,
    `subject_asserted` is True when the subject is the CRM's word about an
    author rather than a verified token's `sub`, and `token_budget` picks the
    budget the model calls are charged to.
    """

    tenant: str
    subject: str
    database: str
    request_id: str
    principal: Principal = "user"
    subject_asserted: bool = False
    token_budget: TokenBudget = "live"


@dataclass(frozen=True)
class RequestContext:
    tenant: str
    subject: str
    database: str
    roles: tuple[str, ...]
    permissions: frozenset[str]
    request_id: str
    principal: Principal = "user"

    @classmethod
    def from_identity(
        cls,
        identity: Identity,
        *,
        permissions: frozenset[str],
        request_id: str,
        principal: Principal = "user",
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
            principal=principal,
        )

    @classmethod
    def for_admitted_job(cls, tenant: str, *, request_id: str) -> RequestContext:
        """The CRM's context for work its service chain already admitted: a
        call job (register item 50). `tenant` is the one the push's gates
        verified, read back from the job key it was stored under, never from
        the job's content; the only way a worker reaches a scope."""
        return cls(
            tenant=tenant,
            subject="service",
            database="",
            roles=(),
            permissions=frozenset(),
            request_id=request_id,
            principal="service",
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
        return self._scope(self.subject, subject_asserted=False, budget="live")

    def scope_for_author(
        self, author_id: int, *, budget: TokenBudget = "live"
    ) -> TenantScope:
        """The scope for a judgement the CRM makes ON BEHALF OF an author.

        Service principal only: the subject is `author:<id>`, the CRM's word
        and not a verified token's, so it is marked asserted. Per-user limits
        (question caps, the user token budget) then key on the author. A user
        principal is refused -- a person's token must never name somebody else.
        """
        if self.principal != "service":
            raise PrincipalMismatchError()
        return self._scope(f"author:{author_id}", subject_asserted=True, budget=budget)

    def _scope(
        self, subject: str, *, subject_asserted: bool, budget: TokenBudget
    ) -> TenantScope:
        """THE construction site: every scope carries the verified tenant."""
        return TenantScope(
            tenant=self.tenant,
            subject=subject,
            database=self.database,
            request_id=self.request_id,
            principal=self.principal,
            subject_asserted=subject_asserted,
            token_budget=budget,
        )
