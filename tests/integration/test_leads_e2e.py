"""End-to-end test of LeadsClient against a realistic fake backend, through
the REAL HttpxTransport (not the mocked Transport the hermetic unit suite
uses). Exercises real httpx request/response handling, the watchdog, and
schema validation together.

SEPARATE from the hermetic suite on purpose: excluded from the default
`pytest` run via the `integration` marker (see pyproject.toml). Run with:

    uv run pytest -m integration --no-cov

(--no-cov: the coverage gate is sized for the full hermetic suite; running
only this handful of tests would fail it for an unrelated reason.)
"""

from __future__ import annotations

import httpx
import pytest

import dodeal_ai.tools.httpx_transport as httpx_transport_module
from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.tools.httpx_transport import HttpxTransport
from dodeal_ai.tools.leads import LeadsClient
from tests.integration.fake_backend import EXPECTED_API_KEY, FakeBackend

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture(autouse=True)
def _route_httpx_to_fake_backend(monkeypatch, fake_backend: FakeBackend):
    """The ONLY thing patched: which transport httpx.AsyncClient uses. Every
    line HttpxTransport.get_json() runs is the real, unmodified production
    code path."""

    class _ASGIAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.ASGITransport(app=fake_backend.app)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx_transport_module.httpx, "AsyncClient", _ASGIAsyncClient)


def _settings(dd_api_key: str = EXPECTED_API_KEY) -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key="test-key",
        dd_api_key=dd_api_key,
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


async def test_get_leads_through_real_httpx_transport(fake_backend):
    client = LeadsClient(HttpxTransport(), _settings())
    leads = await client.get_leads(_context())
    assert [lead.id for lead in leads] == [1, 2]
    assert leads[0].name == "Acme Corp"
    assert fake_backend.last_dd_api_key == EXPECTED_API_KEY


async def test_get_lead_returns_single_lead():
    client = LeadsClient(HttpxTransport(), _settings())
    lead = await client.get_lead(_context(), 1)
    assert lead.id == 1
    assert lead.name == "Acme Corp"


async def test_get_lead_notes_returns_notes_newest_first_order_preserved():
    client = LeadsClient(HttpxTransport(), _settings())
    notes = await client.get_lead_notes(_context(), 1)
    assert len(notes) == 2
    assert notes[0].author == "Jane Doe"
    assert notes[1].author is None  # deleted-author case, nullable


async def test_empty_notes_list_is_not_an_error():
    client = LeadsClient(HttpxTransport(), _settings())
    notes = await client.get_lead_notes(_context(), 2)
    assert notes == []


async def test_malformed_response_fails_closed_through_real_http():
    client = LeadsClient(HttpxTransport(), _settings())
    with pytest.raises(OutputValidationError):
        await client.get_lead(_context(), 999)


async def test_wrong_dd_api_key_is_rejected_and_fails_closed(fake_backend):
    client = LeadsClient(HttpxTransport(), _settings(dd_api_key="wrong-key"))
    with pytest.raises(ExternalCallError):
        await client.get_leads(_context())
    assert fake_backend.last_dd_api_key == "wrong-key"


async def test_fake_backend_returns_401_with_no_key_header_at_all(fake_backend):
    # Direct check of the fake backend's own spec fidelity (DD-API-KEY
    # required, no fallback) -- LeadsClient always sends some value for the
    # header, so this specific case (header truly absent) is exercised
    # directly against the fake app rather than through the client.
    transport = httpx.ASGITransport(app=fake_backend.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://nasir3.dodealcrm.com"
    ) as raw_client:
        response = await raw_client.get("/api/service/leads")
    assert response.status_code == 401
