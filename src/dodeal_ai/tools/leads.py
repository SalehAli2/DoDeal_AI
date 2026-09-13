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
    dodeal_ai.schemas.lead.
  - The call is wrapped by the watchdog (timeout, retry-once, fail closed).

All three methods take a TenantScope (core/context.py), NOT a RequestContext:
design note 0001, Decision 1. A tool needs to know whose data it is fetching,
never what the caller is allowed to do -- and the scope is the shape a
service-token or job-payload principal can also produce.

TODAY the watchdog collapses every backend failure into ExternalCallError, so
a 404 for a missing lead is indistinguishable here from a 500 or a timeout.
That is audit finding H2, planned for step 4 (typed backend errors +
retry_on). Callers must not infer "not found" from an ExternalCallError.
"""

from __future__ import annotations

from typing import Protocol

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.resilience import call_with_watchdog
from dodeal_ai.core.validation import validate_output
from dodeal_ai.schemas.lead import (
    Lead,
    LeadListResponse,
    LeadNote,
    LeadNotesResponse,
    LeadResponse,
)
from dodeal_ai.tools.httpx_transport import HttpxTransport
from dodeal_ai.tools.keys import TenantKeyResolver, get_key_resolver


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
    ):
        self._transport = transport
        self._key_resolver = key_resolver
        self._settings = settings or get_settings()

    def _base_url(self, subdomain: str) -> str:
        s = self._settings
        return f"{s.backend_scheme}://{subdomain}.{s.backend_base_domain}/api/service"

    def _headers(self, tenant: str) -> dict[str, str]:
        # The ONLY .get_secret_value() call in src/ -- the key exists as
        # plaintext only for the instant it takes to build this header.
        return {"DD-API-KEY": self._key_resolver.resolve(tenant).get_secret_value()}

    async def _get(self, url: str, label: str, tenant: str) -> object:
        # Resolve the key (and so fail closed on an unknown tenant) BEFORE
        # _fetch is defined/handed to the watchdog: a missing key is a
        # configuration fault, not a transient external failure, so it must
        # never be retried and must never come back wrapped as an
        # ExternalCallError.
        headers = self._headers(tenant)

        async def _fetch() -> object:
            return await self._transport.get_json(url, headers)

        return await call_with_watchdog(_fetch, label=label)

    async def get_leads(self, scope: TenantScope) -> list[Lead]:
        """Fetch the tenant's leads. The subdomain is taken from the scope the
        gates produced, so a caller cannot fetch another tenant's data.
        """
        url = f"{self._base_url(scope.tenant)}/leads"
        raw = await self._get(url, label="tool.get_leads", tenant=scope.tenant)
        response = validate_output(LeadListResponse, raw, label="tool.get_leads")
        return response.data

    async def get_lead(self, scope: TenantScope, lead_id: int) -> Lead:
        """Fetch a single lead by id, scoped to the request's tenant."""
        url = f"{self._base_url(scope.tenant)}/leads/{lead_id}"
        raw = await self._get(url, label="tool.get_lead", tenant=scope.tenant)
        response = validate_output(LeadResponse, raw, label="tool.get_lead")
        return response.data

    async def get_lead_notes(self, scope: TenantScope, lead_id: int) -> list[LeadNote]:
        """Fetch a lead's notes, newest first. An empty list is a valid
        result (a lead with no notes), not an error."""
        url = f"{self._base_url(scope.tenant)}/leads/{lead_id}/notes"
        raw = await self._get(url, label="tool.get_lead_notes", tenant=scope.tenant)
        response = validate_output(LeadNotesResponse, raw, label="tool.get_lead_notes")
        return response.data


def get_leads_client() -> LeadsClient:
    """FastAPI dependency. Same shape as get_verifier() and get_key_resolver():
    tests override it at the route via app.dependency_overrides, never by
    patching a module global.

    Builds a fresh client (and so a fresh AsyncClient per call, inside
    HttpxTransport) each time. That is audit finding M1, planned for step 4
    along with a lifespan-owned pooled AsyncClient; pooling it here would mean
    inventing connection lifecycle ownership in a phase that is only wiring the
    seam.
    """
    return LeadsClient(HttpxTransport(), get_key_resolver(), get_settings())
