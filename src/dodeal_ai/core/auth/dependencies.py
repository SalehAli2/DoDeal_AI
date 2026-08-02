"""The gate chain, as FastAPI dependencies.

Each gate is a SEPARATE small Depends() so the exit demo can prove them in
isolation. They run in order and thread state forward:

    verify (Gate 1)  -> Identity        | 401 on failure
    tenant (Gate 2)  -> tenant_id       | 403 on mismatch
    role   (Gate 3)  -> RequestContext  | 403 on missing permission

The dependencies are thin adapters: they pull raw values off the HTTP request
(Authorization header, X-Tenant-ID header) and call the already-tested gate
functions. All real logic lives in verify.py / tenancy.py / permissions.py and
is unit-tested without HTTP.

Failures are converted to GENERIC HTTPExceptions here — the client sees 401/403
with a bland detail; the specific reason_code goes to the audit log (Step 8),
never to the client.

Per-endpoint permission: a route declares what it needs via
require_context(permission). See _probe.py for the pattern.
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
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.tenancy import TenantMismatchError, check_tenant


def get_verifier() -> TokenVerifier:
    """The swap point. Today: JwtVerifier (reads Settings). When Q1 resolves,
    this returns a different TokenVerifier and nothing else changes. Overridable
    in tests via app.dependency_overrides."""
    return JwtVerifier()


def _bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    """Extract the bearer token from the Authorization header. A missing or
    malformed header is a generic 401 — same shape as any other auth failure."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return authorization[len("bearer ") :].strip()


def gate1_identity(
    request: Request,
    token: Annotated[str, Depends(_bearer_token)],
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
) -> Identity:
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        identity = verify_token(token, verifier)
    except AuthError as exc:
        # tenant_id unknown here: token not trusted, so we log null, not a guess.
        audit(
            decision="deny",
            gate="auth",
            request_id=request_id,
            reason_code=exc.reason_code,
            tenant_id=None,
        )
        raise HTTPException(status_code=401, detail="Unauthorized")
    audit(
        decision="allow",
        gate="auth",
        request_id=request_id,
        reason_code="ok",
        tenant_id=identity.tenant_id,
    )
    return identity


def gate2_tenant(
    request: Request,
    identity: Annotated[Identity, Depends(gate1_identity)],
    x_tenant_id: Annotated[str | None, Header()] = None,
) -> Identity:
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        check_tenant(identity, x_tenant_id)
    except TenantMismatchError as exc:
        audit(
            decision="deny",
            gate="tenancy",
            request_id=request_id,
            reason_code=exc.reason_code,
            tenant_id=identity.tenant_id,  # authoritative token tenant, safe
        )
        raise HTTPException(status_code=403, detail="Forbidden")
    audit(
        decision="allow",
        gate="tenancy",
        request_id=request_id,
        reason_code="ok",
        tenant_id=identity.tenant_id,
    )
    return identity


def build_context(
    request: Request,
    identity: Annotated[Identity, Depends(gate2_tenant)],
) -> RequestContext:
    """Assemble the immutable RequestContext once Gates 1-2 have passed.
    Permissions are resolved here (Gate 3's table); the per-endpoint permission
    check happens in require_context below. request_id comes from middleware
    (request.state.request_id) if present, else a placeholder until middleware
    lands."""
    permissions = resolve_permissions(identity)
    request_id = getattr(request.state, "request_id", "unknown")
    return RequestContext.from_identity(
        identity, permissions=permissions, request_id=request_id
    )


def require_context(permission: str):
    def _dependency(
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
                tenant_id=context.tenant_id,
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        audit(
            decision="allow",
            gate="authz",
            request_id=context.request_id,
            reason_code="ok",
            tenant_id=context.tenant_id,
        )
        return context

    return _dependency