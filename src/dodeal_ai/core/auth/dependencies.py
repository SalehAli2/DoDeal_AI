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

THE SERVICE CHAIN (register item D1) is a second, parallel chain for the CRM's
own server-to-server token: service_gate1_identity (ServiceTokenVerifier, audit
gate "service_auth") -> service_gate2_tenant (the same Host match, audit gate
"service_tenancy") -> build_service_context (principal "service") ->
service_gate4_cost (the TENANT request counter only). A user token is 401 on it,
and a service token is 401 on the user chain: the verifiers share no key. The
history route ends in service_gate4_history_cost instead (register item 127),
and the brief, measures and admin routes in service_gate4_reads_cost (item
153): the same chain, each counted on the tenant's own counter for that work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request

from dodeal_ai.core.audit.logger import audit
from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.auth.service import ServiceTokenVerifier
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
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.cost.limiter import (
    CostLimitError,
    enforce_cost,
    enforce_history_cost,
    enforce_reads_cost,
    enforce_tenant_cost,
)
from dodeal_ai.core.tenancy import TenantMismatchError, check_tenant

# Register item 12: the verifier and the Settings object it was built from.
# Matched by identity, so a new Settings (a cleared cache) builds a new one.
_verifier_cache: tuple[Settings, JwtVerifier] | None = None


def get_verifier() -> TokenVerifier:
    """The swap point. Today: JwtVerifier (reads Settings). When Q1 resolves,
    this returns a different TokenVerifier and nothing else changes. Overridable
    in tests via app.dependency_overrides.

    Built once per Settings object, not per request. FastAPI runs this in its
    threadpool, so two first requests may each build one; either is correct."""
    global _verifier_cache
    settings = get_settings()
    cached = _verifier_cache
    if cached is None or cached[0] is not settings:
        cached = (settings, JwtVerifier(settings))
        _verifier_cache = cached
    return cached[1]


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


# --- the service chain (register item D1) ----------------------------------

# The service verifier and the Settings it was built from, as for the user one.
_service_verifier_cache: tuple[Settings, ServiceTokenVerifier] | None = None


def get_service_verifier() -> ServiceTokenVerifier:
    """The service chain's swap point, built once per Settings object and
    overridable through app.dependency_overrides like get_verifier."""
    global _service_verifier_cache
    settings = get_settings()
    cached = _service_verifier_cache
    if cached is None or cached[0] is not settings:
        cached = (settings, ServiceTokenVerifier(settings))
        _service_verifier_cache = cached
    return cached[1]


async def service_gate1_identity(
    request: Request,
    token: Annotated[str, Depends(_bearer_token)],
    verifier: Annotated[ServiceTokenVerifier, Depends(get_service_verifier)],
) -> Identity:
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        identity = verifier.verify(token)
    except AuthError as exc:
        audit(
            decision="deny",
            gate="service_auth",
            request_id=request_id,
            reason_code=exc.reason_code,
            tenant=None,
        )
        raise HTTPException(status_code=401, detail="Unauthorized")
    audit(
        decision="allow",
        gate="service_auth",
        request_id=request_id,
        reason_code="ok",
        tenant=identity.tenant,
    )
    return identity


async def service_gate2_tenant(
    request: Request,
    identity: Annotated[Identity, Depends(service_gate1_identity)],
    host: Annotated[str | None, Header()] = None,
) -> Identity:
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        check_tenant(identity, host, get_settings().inbound_base_domain)
    except TenantMismatchError as exc:
        audit(
            decision="deny",
            gate="service_tenancy",
            request_id=request_id,
            reason_code=exc.reason_code,
            tenant=identity.tenant,
        )
        raise HTTPException(status_code=403, detail="Forbidden")
    audit(
        decision="allow",
        gate="service_tenancy",
        request_id=request_id,
        reason_code="ok",
        tenant=identity.tenant,
    )
    return identity


async def build_service_context(
    request: Request,
    identity: Annotated[Identity, Depends(service_gate2_tenant)],
) -> RequestContext:
    """A context whose principal is the CRM. No permissions: Gate 3 is parked
    on this chain exactly as on the user one."""
    request_id = getattr(request.state, "request_id", "unknown")
    return RequestContext.from_identity(
        identity,
        permissions=frozenset(),
        request_id=request_id,
        principal="service",
    )


def _names_this_service(token: str, settings: Settings) -> bool:
    """Does the UNVERIFIED token name this service as its audience? It only
    picks which chain verifies the token, and each chain verifies it in full,
    so a forged `aud` routes a token to a chain that then refuses it."""
    try:
        audience = jwt.decode(token, options={"verify_signature": False}).get("aud")
    except jwt.InvalidTokenError:
        return False
    names = audience if isinstance(audience, list) else [audience]
    return settings.service_jwt_audience in names


async def gate4_either_principal(
    request: Request,
    token: Annotated[str, Depends(_bearer_token)],
    user_verifier: Annotated[TokenVerifier, Depends(get_verifier)],
    service_verifier: Annotated[ServiceTokenVerifier, Depends(get_service_verifier)],
    host: Annotated[str | None, Header()] = None,
) -> RequestContext:
    """ONE dependency for a route either principal may call (/meta/versions).

    A token addressed to this service takes the whole service chain; anything
    else takes the whole user chain. Same gates, same audit lines, same
    refusals as the chain alone -- this only chooses between them.
    """
    if _names_this_service(token, service_verifier.settings):
        identity = await service_gate1_identity(request, token, service_verifier)
        identity = await service_gate2_tenant(request, identity, host)
        return await service_gate4_cost(
            request, await build_service_context(request, identity)
        )
    identity = await gate1_identity(request, token, user_verifier)
    identity = await gate2_tenant(request, identity, host)
    return await gate4_cost(request, await build_context(request, identity))


async def service_gate4_cost(
    request: Request,
    context: Annotated[RequestContext, Depends(build_service_context)],
) -> RequestContext:
    """Gate 4 for the service chain: the TENANT counter only. The token names no
    person, so a per-user request cap here would be one bucket for everyone."""
    return await _service_gate4(request, context, enforce_tenant_cost)


async def service_gate4_history_cost(
    request: Request,
    context: Annotated[RequestContext, Depends(build_service_context)],
) -> RequestContext:
    """Gate 4 for history judgements (register item 127): the tenant's HISTORY
    request counter alone, so a backfill never moves `cost:tenant`."""
    return await _service_gate4(request, context, enforce_history_cost)


async def service_gate4_reads_cost(
    request: Request,
    context: Annotated[RequestContext, Depends(build_service_context)],
) -> RequestContext:
    """Gate 4 for the brief, measures and admin routes (register item 153): the
    tenant's READS request counter alone, so reads never move `cost:tenant`."""
    return await _service_gate4(request, context, enforce_reads_cost)


async def _service_gate4(
    request: Request,
    context: RequestContext,
    enforce: Callable[[str], Awaitable[None]],
) -> RequestContext:
    """One service Gate 4: move the counter `enforce` owns, audit, 429 over."""
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        await enforce(context.tenant)
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
