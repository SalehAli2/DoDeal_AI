"""Backend tool client for lead data (read-only).

Fetches lead and note data from the tenant's CRM backend over HTTP. The
service has no direct database access; this client is the only path to lead
data.

Endpoints (base https://<subdomain>.<base_domain>/api/service):
  - GET /leads              paginated list, newest first
  - GET /leads/{id}         single lead
  - GET /leads/{id}/notes   that lead's notes, newest first

Request flow, all three methods:
  - URL is built from the request's authoritative subdomain:
    <scheme>://<subdomain>.<base_domain>/api/service/...
    The scheme is DODEAL_BACKEND_SCHEME and is https everywhere but the demo,
    where a laptop-served fake CRM has no certificate. See Settings.
  - Authentication uses the DD-API-KEY header; these endpoints take no JWT.
    The key is per-tenant (a key is valid only against its own tenant host),
    resolved for the request's tenant via TenantKeyResolver (tools/keys.py)
    before any network call -- an unknown tenant fails closed and is never
    retried or wrapped by the watchdog.
  - The response is validated against the confirmed schemas in
    dodeal_ai.schemas.lead. A list is validated ROW BY ROW: a bad row is
    dropped and counted, never the page (register item 90). A wrong response
    shape is BackendEnvelopeInvalid, logged backend_envelope_invalid (F1).
  - The call is wrapped by the watchdog (timeout, fail closed), retrying once
    only on a connect error, a read timeout, a 5xx or a 429 (register item 89).

All three methods take a TenantScope (core/context.py), NOT a RequestContext:
design note 0001, Decision 1. A tool needs to know whose data it is fetching,
never what the caller is allowed to do -- and the scope is the shape a
service-token or job-payload principal can also produce.

TYPED FAILURES (register item 89, audit H2). 401, 403, 404 and any other 4xx
but 429 leave as the typed errors in tools/errors.py, never retried. Everything
else that fails -- after its one retry, where the rule allows one -- is still
ExternalCallError, and callers must not infer "not found" from it.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import Protocol

import httpx
from fastapi import Request
from pydantic import BaseModel, ValidationError

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.errors import BackendUnavailableError
from dodeal_ai.core.resilience import ExternalCallError, call_with_watchdog
from dodeal_ai.core.validation import OutputValidationError, validate_output
from dodeal_ai.schemas.lead import Lead, LeadNote, LeadResponse, RowsEnvelope
from dodeal_ai.tools.errors import (
    BackendEnvelopeInvalid,
    BackendError,
    BackendForbidden,
    BackendNotFound,
    BackendRejected,
    BackendStatusError,
    BackendUnauthorized,
)
from dodeal_ai.tools.httpx_transport import HttpxTransport
from dodeal_ai.tools.keys import TenantKeyResolver, get_key_resolver

_logger = logging.getLogger("dodeal_ai.tools")

GET_LEADS_LABEL = "tool.get_leads"
GET_LEAD_LABEL = "tool.get_lead"
GET_LEAD_NOTES_LABEL = "tool.get_lead_notes"

# The one retry waits 100-400 ms, jittered so pods that failed together do not
# retry together (register item 89).
RETRY_JITTER_MIN_SECONDS = 0.1
RETRY_JITTER_MAX_SECONDS = 0.4
# The longest Retry-After a 429 may ask for and still get its retry. A longer
# ask fails closed: retrying early would only meet the same 429.
RETRY_AFTER_CAP_SECONDS = 2.0

# Failures that say nothing about the request itself, so one retry may help.
_TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    TimeoutError,
)


def _jitter() -> float:
    return random.uniform(RETRY_JITTER_MIN_SECONDS, RETRY_JITTER_MAX_SECONDS)


def backend_retry_delay(exc: Exception, jitter: Callable[[], float]) -> float | None:
    """The wait before the one retry of a backend read, or None for no retry.

    Connect error, read timeout and 5xx retry after jitter. 429 retries after
    its Retry-After when that is at most the cap, after jitter when it is
    absent. Every other 4xx, and anything not listed, is never retried.
    """
    if isinstance(exc, BackendStatusError):
        if exc.status == httpx.codes.TOO_MANY_REQUESTS:
            if exc.retry_after is None:
                return jitter()
            if exc.retry_after <= RETRY_AFTER_CAP_SECONDS:
                return exc.retry_after
            return None
        return jitter() if exc.status >= httpx.codes.INTERNAL_SERVER_ERROR else None
    if isinstance(exc, _TRANSIENT_ERRORS):
        return jitter()
    return None


class Transport(Protocol):
    """Minimal async HTTP transport seam. The production implementation wraps
    httpx.AsyncClient; tests supply a mock. Returns the decoded JSON body."""

    async def get_json(self, url: str, headers: dict[str, str]) -> object: ...


class LeadsClient:
    """Read-only client for tenant lead and note data."""

    def __init__(
        self,
        transport: Transport,
        key_resolver: TenantKeyResolver,
        settings: Settings | None = None,
        *,
        jitter: Callable[[], float] = _jitter,
    ):
        self._transport = transport
        self._key_resolver = key_resolver
        self._settings = settings or get_settings()
        self._jitter = jitter

    def _base_url(self, subdomain: str) -> str:
        s = self._settings
        return f"{s.backend_scheme}://{subdomain}.{s.backend_base_domain}/api/service"

    def _headers(self, tenant: str) -> dict[str, str]:
        # One of the two .get_secret_value() calls on a DD-API-KEY in src/ (the
        # other is tools/crm_reads.py's): plaintext only while the header is built.
        return {"DD-API-KEY": self._key_resolver.resolve(tenant).get_secret_value()}

    async def _get(
        self, url: str, label: str, tenant: str, deadline: float | None
    ) -> object:
        # Resolve the key (and so fail closed on an unknown tenant) BEFORE
        # _fetch is defined/handed to the watchdog: a missing key is a
        # configuration fault, not a transient external failure, so it must
        # never be retried and must never come back wrapped as an
        # ExternalCallError.
        headers = self._headers(tenant)

        async def _fetch() -> object:
            return await self._transport.get_json(url, headers)

        try:
            return await call_with_watchdog(
                _fetch,
                label=label,
                retry_delay=lambda exc: backend_retry_delay(exc, self._jitter),
                deadline=deadline,
            )
        except ExternalCallError as exc:
            typed = _typed_failure(exc.cause, label, tenant)
            if typed is None:
                raise
            raise typed from None

    async def get_leads(
        self, scope: TenantScope, *, deadline: float | None = None
    ) -> list[Lead]:
        """Fetch the tenant's leads. The subdomain is taken from the scope the
        gates produced, so a caller cannot fetch another tenant's data.
        """
        url = f"{self._base_url(scope.tenant)}/leads"
        raw = await self._get(url, GET_LEADS_LABEL, scope.tenant, deadline)
        envelope = _envelope(RowsEnvelope, raw, GET_LEADS_LABEL, scope.tenant)
        return _valid_rows(Lead, envelope.data, GET_LEADS_LABEL)

    async def get_lead(
        self, scope: TenantScope, lead_id: int, *, deadline: float | None = None
    ) -> Lead:
        """Fetch a single lead by id, scoped to the request's tenant."""
        url = f"{self._base_url(scope.tenant)}/leads/{lead_id}"
        raw = await self._get(url, GET_LEAD_LABEL, scope.tenant, deadline)
        response = _envelope(LeadResponse, raw, GET_LEAD_LABEL, scope.tenant)
        return response.data

    async def get_lead_notes(
        self, scope: TenantScope, lead_id: int, *, deadline: float | None = None
    ) -> list[LeadNote]:
        """Fetch a lead's notes, newest first. An empty list is a valid
        result (a lead with no notes), not an error."""
        url = f"{self._base_url(scope.tenant)}/leads/{lead_id}/notes"
        raw = await self._get(url, GET_LEAD_NOTES_LABEL, scope.tenant, deadline)
        envelope = _envelope(RowsEnvelope, raw, GET_LEAD_NOTES_LABEL, scope.tenant)
        return _valid_rows(LeadNote, envelope.data, GET_LEAD_NOTES_LABEL)


def _envelope[M: BaseModel](schema: type[M], raw: object, label: str, tenant: str) -> M:
    """The response in its documented shape, or BackendEnvelopeInvalid after one
    backend_envelope_invalid line: tenant, label and a count, never a value."""
    try:
        return validate_output(schema, raw, label=label)
    except OutputValidationError as exc:
        _logger.warning(
            "backend_envelope_invalid",
            extra={
                "reason_code": "backend_envelope_invalid",
                "tenant": tenant,
                "label": label,
                "error_count": exc.error_count,
            },
        )
        raise BackendEnvelopeInvalid(label, exc.errors) from None


def _valid_rows[M: BaseModel](
    schema: type[M], rows: list[object], label: str
) -> list[M]:
    """Every row that validates, in page order. The rest are dropped and counted
    on one WARNING: the count and the endpoint, never a value or a field name
    (register item 90). A dropped note is then simply not found by id."""
    valid: list[M] = []
    for row in rows:
        try:
            valid.append(schema.model_validate(row))
        except ValidationError:
            continue
    rejected = len(rows) - len(valid)
    if rejected:
        _logger.warning(
            "backend_rejected_rows",
            extra={
                "reason_code": "backend_rejected_rows",
                "count": rejected,
                "endpoint": label,
            },
        )
    return valid


def _typed_failure(cause: Exception, label: str, tenant: str) -> BackendError | None:
    """The typed error for a 4xx other than 429, or None to keep the
    ExternalCallError. 401 and 403 log at ERROR: a refused key is a
    provisioning fault that no retry will fix."""
    if not isinstance(cause, BackendStatusError):
        return None
    status = cause.status
    if status == httpx.codes.TOO_MANY_REQUESTS or not 400 <= status < 500:
        return None
    if status == httpx.codes.NOT_FOUND:
        return BackendNotFound(label, status)
    if status not in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return BackendRejected(label, status)
    error: BackendError = (
        BackendUnauthorized(label, status)
        if status == httpx.codes.UNAUTHORIZED
        else BackendForbidden(label, status)
    )
    _logger.error(
        error.reason_code,
        extra={
            "reason_code": error.reason_code,
            "tenant": tenant,
            "label": label,
            "status": status,
        },
    )
    return error


def get_leads_client(request: Request) -> LeadsClient:
    """FastAPI dependency: a client over the ONE pooled CRM AsyncClient the
    lifespan built (register item 4, audit M1). Tests override it at the route.

    No client on app.state means the lifespan did not run: 503
    backend_unavailable, never a private pool built here.
    """
    http: httpx.AsyncClient | None = getattr(request.app.state, "crm_http", None)
    if http is None:
        raise BackendUnavailableError()
    return LeadsClient(HttpxTransport(http), get_key_resolver(), get_settings())
