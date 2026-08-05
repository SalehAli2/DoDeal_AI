"""Leads client: builds tenant URL, sends DD-API-KEY, reads posts.data,
validates the response, and fails closed on a malformed one."""
from __future__ import annotations

import pytest

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.tools.leads import LeadsClient


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    from dodeal_ai.core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    
def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key="test-key",
        dd_api_key="test-dd-api-key",
        backend_base_domain="dodealcrm.com",
    )


def _context(tenant: str = "nasir3") -> RequestContext:
    return RequestContext(
        tenant=tenant,
        subject="42",
        database="crm_nasir3",
        roles=(),
        permissions=frozenset(),
        request_id="req-1",
    )


class MockTransport:
    """Records the URL and headers it was called with, returns a canned body."""

    def __init__(self, body: object):
        self._body = body
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None

    async def get_json(self, url: str, headers: dict[str, str]) -> object:
        self.last_url = url
        self.last_headers = headers
        return self._body


def _valid_body(leads: list[dict]) -> dict:
    return {"status": True, "posts": {"current_page": 1, "data": leads}}


@pytest.mark.asyncio
async def test_builds_tenant_url_from_context_subdomain():
    transport = MockTransport(_valid_body([{"id": 1}]))
    client = LeadsClient(transport, _settings())
    await client.get_leads(_context("nasir3"))
    assert transport.last_url == "https://nasir3.dodealcrm.com/api/leads"


async def test_sends_dd_api_key_header():
    transport = MockTransport(_valid_body([{"id": 1}]))
    client = LeadsClient(transport, _settings())
    await client.get_leads(_context())
    assert transport.last_headers == {"DD-API-KEY": "test-dd-api-key"}


async def test_returns_leads_from_posts_data():
    transport = MockTransport(_valid_body([{"id": 1}, {"id": 2}]))
    client = LeadsClient(transport, _settings())
    leads = await client.get_leads(_context())
    assert [lead.id for lead in leads] == [1, 2]


async def test_malformed_response_fails_closed():
    # Old shape / wrong wrapper must be rejected, not surfaced.
    transport = MockTransport({"success": True, "data": []})
    client = LeadsClient(transport, _settings())
    with pytest.raises(OutputValidationError):
        await client.get_leads(_context())


async def test_different_tenant_hits_different_url():
    transport = MockTransport(_valid_body([{"id": 1}]))
    client = LeadsClient(transport, _settings())
    await client.get_leads(_context("beta"))
    assert transport.last_url == "https://beta.dodealcrm.com/api/leads"