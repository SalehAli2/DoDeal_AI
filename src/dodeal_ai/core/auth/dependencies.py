"""The gate chain, as FastAPI dependencies.

Each gate is a separate Depends() so it can be tested in isolation. They run in
order and thread state forward:

    Gate 1 (auth)     verify token   -> Identity          | 401 on failure
    Gate 2 (tenancy)  subdomain match-> Identity          | 403 on mismatch
    Gate 3 (authz)    permission     -> RequestContext     | PARKED (see below)

The dependencies are thin adapters: they read raw values from the request (the
Authorization and Host headers) and call the gate functions in verify.py and
tenancy.py, which hold the logic and are unit-tested without HTTP.

Failures are converted to generic HTTPExceptions here; the client sees 401/403
with a bland detail, and the specific reason code goes only to the audit log.

Gate 3 (permissions) is currently parked: the token carries no roles and the
permission model is undecided, so the live chain ends at build_context (auth +
tenancy). require_context / require_permission remain here, tested in isolation,
ready to wire when the permission model is confirmed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from dodeal_ai.core.audit.logger import audit
from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.auth.verify import (
    AuthError,
    JwtVerifier,
    TokenVerifier,
    verify_token,
)
from dodeal_ai.core.authz.permissions import (
    PermissionDeniedError,
    require_permission,
    resolve_permissions,
)
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.cost.limiter import (
    CostLimitError,
    enforce_cost,
)
from dodeal_ai.core.tenancy import TenantMismatchError, check_tenant


def get_verifier() -> TokenVerifier:
    """The swap point. Today: JwtVerifier (reads Settings). When Q1 resolves,
    this returns a different TokenVerifier and nothing else changes. Overridable
    in tests via app.dependency_overrides."""
    return JwtVerifier()


async def _bearer_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Extract the bearer token from the Authorization header. A missing or
    malformed header is a generic 401 — same shape as any other auth failure."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return authorization[len("bearer ") :].strip()


async def gate1_identity(
    request: Request,
    token: Annotated[str, Depends(_bearer_token)],
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
) -> Identity:
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        identity = verify_token(token, verifier)
    except AuthError as exc:
        # tenant unknown here: token not trusted, so we log null, not a guess.
        audit(
            decision="deny",
            gate="auth",
            request_id=request_id,
            reason_code=exc.reason_code,
            tenant=None,
        )
        raise HTTPException(status_code=401, detail="Unauthorized")
    audit(
        decision="allow",
        gate="auth",
        request_id=request_id,
        reason_code="ok",
        tenant=identity.tenant,
    )
    return identity


async def gate2_tenant(
    request: Request,
    identity: Annotated[Identity, Depends(gate1_identity)],
    host: Annotated[str | None, Header()] = None,
) -> Identity:
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        check_tenant(identity, host, get_settings().inbound_base_domain)
    except TenantMismatchError as exc:
        audit(
            decision="deny",
            gate="tenancy",
            request_id=request_id,
            reason_code=exc.reason_code,
            tenant=identity.tenant,
        )
        raise HTTPException(status_code=403, detail="Forbidden")
    audit(
        decision="allow",
        gate="tenancy",
        request_id=request_id,
        reason_code="ok",
        tenant=identity.tenant,
    )
    return identity


async def build_context(
    request: Request,
    identity: Annotated[Identity, Depends(gate2_tenant)],
) -> RequestContext:
    # Gate 3 (permissions) is PARKED: the token carries no roles and the
    # permission-enforcement approach is not yet decided. Until it is, the chain
    # runs Gate 1 (auth) and Gate 2 (tenancy) only. resolve_permissions returns
    # an empty set here; no permission is enforced on the live route.
    permissions = resolve_permissions(identity)
    request_id = getattr(request.state, "request_id", "unknown")
    return RequestContext.from_identity(
        identity, permissions=permissions, request_id=request_id
    )


def require_context(permission: str):
    async def _dependency(
        request: Request,
        context: Annotated[RequestContext, Depends(build_context)],
    ) -> RequestContext:
        try:
            require_permission(context, permission)
        except PermissionDeniedError as exc:
            audit(
                decision="deny",
                gate="authz",
                request_id=context.request_id,
                reason_code=exc.reason_code,
                tenant=context.tenant,
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        audit(
            decision="allow",
            gate="authz",
            request_id=context.request_id,
            reason_code="ok",
            tenant=context.tenant,
        )
        return context

    return _dependency


async def gate4_cost(
    request: Request,
    context: Annotated[RequestContext, Depends(build_context)],
) -> RequestContext:
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        await enforce_cost(context.tenant, context.subject)
    except CostLimitError as exc:
        audit(
            decision="deny",
            gate="cost",
            request_id=request_id,
            reason_code=exc.reason_code,
            tenant=context.tenant,
        )
        raise HTTPException(status_code=429, detail="Too Many Requests")
    audit(
        decision="allow",
        gate="cost",
        request_id=request_id,
        reason_code="ok",
        tenant=context.tenant,
    )
    return context
