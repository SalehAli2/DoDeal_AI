"""Backend tool client for lead data (read-only).

Fetches lead data from the tenant's CRM backend over HTTP. The service has no
direct database access; this client is the only path to lead data.

Structure only at this stage: no live network call and no real key are wired.
The transport is injected, so tests supply a mock returning the confirmed
response shape. The real per-tenant DD-API-KEY and live endpoint are pending
backend provisioning.

Request flow:
  - URL is built from the request's authoritative subdomain:
    https://<subdomain>.<base_domain>/api/leads
  - Authentication uses the DD-API-KEY header (per-tenant).
  - The response is validated against schemas.lead.LeadListResponse; leads are
    read from posts.data.
  - The call is wrapped by the watchdog (timeout, retry-once, fail closed).
"""
from __future__ import annotations

from typing import Protocol

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.resilience import call_with_watchdog
from dodeal_ai.core.validation import validate_output
from schemas.lead import Lead, LeadListResponse


class Transport(Protocol):
    """Minimal async HTTP transport seam. The production implementation wraps
    httpx.AsyncClient; tests supply a mock. Returns the decoded JSON body."""

    async def get_json(self, url: str, headers: dict[str, str]) -> object: ...


class LeadsClient:
    """Read-only client for tenant lead data."""

    def __init__(self, transport: Transport, settings: Settings | None = None):
        self._transport = transport
        self._settings = settings or get_settings()

    def _base_url(self, subdomain: str) -> str:
        return f"https://{subdomain}.{self._settings.backend_base_domain}/api"

    def _headers(self) -> dict[str, str]:
        return {"DD-API-KEY": self._settings.dd_api_key}

    async def get_leads(self, context: RequestContext) -> list[Lead]:
        """Fetch the tenant's leads. The subdomain is taken from the request's
        authoritative context, so a caller cannot fetch another tenant's data.
        """
        url = f"{self._base_url(context.tenant)}/leads"

        async def _fetch() -> object:
            return await self._transport.get_json(url, self._headers())

        raw = await call_with_watchdog(_fetch, label="tool.get_leads")
        response = validate_output(
            LeadListResponse, raw, label="tool.get_leads"
        )
        return response.posts.data