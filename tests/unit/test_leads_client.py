"""Leads client: builds tenant URLs, sends DD-API-KEY, reads leads, a single
lead, and notes from `data`, validates each response, and fails closed on a
malformed one."""

from __future__ import annotations

import ast
import logging
import pathlib
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import RequestContext, TenantScope
from dodeal_ai.core.errors import BackendUnavailableError, NoteNotFoundError
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.tools.keys import BackendKeyError, SettingsKeyResolver
from dodeal_ai.tools.leads import LeadsClient, get_leads_client
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import JudgementDeps, judge_note
from dodeal_ai.units.structured_intelligence.schemas import JudgementRequest
from tests.helpers.fake_llm import FakeLLM


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key="test-key",
        dd_api_keys={"tenant-a": "key-tenant-a", "acme": "key-acme"},
        backend_base_domain="dodealcrm.com",
    )


def _client(transport: MockTransport, settings: Settings | None = None) -> LeadsClient:
    settings = settings or _settings()
    return LeadsClient(transport, SettingsKeyResolver(settings), settings)


def _scope(tenant: str = "tenant-a") -> TenantScope:
    # Built through RequestContext.scope(), the ONE construction site, so this
    # exercises the same narrowing the routes do rather than a parallel shape.
    return RequestContext(
        tenant=tenant,
        subject="42",
        database="crm_tenant_a",
        roles=(),
        permissions=frozenset(),
        request_id="req-1",
    ).scope()


class MockTransport:
    """Records the URL and headers it was called with, returns a canned body,
    and counts how many times it was actually called -- so a test can prove a
    fail-closed path never reaches the network."""

    def __init__(self, body: object):
        self._body = body
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.calls = 0

    async def get_json(self, url: str, headers: dict[str, str]) -> object:
        self.calls += 1
        self.last_url = url
        self.last_headers = headers
        return self._body


def _meta(total: int) -> dict:
    return {"current_page": 1, "per_page": 25, "total": total, "last_page": 1}


def _list_body(leads: list[dict]) -> dict:
    return {"status": True, "data": leads, "meta": _meta(len(leads))}


def _lead_body(lead: dict) -> dict:
    return {"status": True, "data": lead}


def _notes_body(notes: list[dict]) -> dict:
    return {"status": True, "data": notes, "meta": _meta(len(notes))}


async def test_builds_tenant_url_from_scope_subdomain():
    transport = MockTransport(_list_body([{"id": 1}]))
    client = _client(transport)
    await client.get_leads(_scope("tenant-a"))
    assert transport.last_url == "https://tenant-a.dodealcrm.com/api/service/leads"


async def test_sends_dd_api_key_header():
    transport = MockTransport(_list_body([{"id": 1}]))
    client = _client(transport)
    await client.get_leads(_scope())
    assert transport.last_headers == {"DD-API-KEY": "key-tenant-a"}


async def test_returns_leads_from_data():
    transport = MockTransport(_list_body([{"id": 1}, {"id": 2}]))
    client = _client(transport)
    leads = await client.get_leads(_scope())
    assert [lead.id for lead in leads] == [1, 2]


async def test_malformed_response_fails_closed():
    # Old shape / wrong wrapper must be rejected, not surfaced.
    transport = MockTransport({"success": True, "data": []})
    client = _client(transport)
    with pytest.raises(OutputValidationError):
        await client.get_leads(_scope())


async def test_different_tenant_hits_different_url():
    transport = MockTransport(_list_body([{"id": 1}]))
    client = _client(transport)
    await client.get_leads(_scope("acme"))
    assert transport.last_url == "https://acme.dodealcrm.com/api/service/leads"


async def test_get_lead_builds_id_url_and_returns_lead():
    transport = MockTransport(_lead_body({"id": 7, "name": "Acme"}))
    client = _client(transport)
    lead = await client.get_lead(_scope("tenant-a"), 7)
    assert transport.last_url == "https://tenant-a.dodealcrm.com/api/service/leads/7"
    assert lead.id == 7
    assert lead.name == "Acme"


async def test_get_lead_malformed_response_fails_closed():
    transport = MockTransport({"status": True})  # missing data
    client = _client(transport)
    with pytest.raises(OutputValidationError):
        await client.get_lead(_scope(), 7)


async def test_get_lead_notes_builds_url_and_returns_notes():
    notes = [
        {
            "id": 1,
            "note": "Called.",
            "author": "Jane",
            "author_id": 10,
            "createdAt": "2026-01-01T10:00:00+00:00",
        },
    ]
    transport = MockTransport(_notes_body(notes))
    client = _client(transport)
    result = await client.get_lead_notes(_scope("tenant-a"), 7)
    assert (
        transport.last_url == "https://tenant-a.dodealcrm.com/api/service/leads/7/notes"
    )
    assert len(result) == 1
    assert result[0].author == "Jane"


async def test_get_lead_notes_empty_list_is_not_an_error():
    transport = MockTransport(_notes_body([]))
    client = _client(transport)
    result = await client.get_lead_notes(_scope(), 7)
    assert result == []


async def test_get_lead_notes_tolerates_null_author():
    notes = [
        {
            "id": 2,
            "note": "Voicemail.",
            "author": None,
            "author_id": 11,
            "createdAt": "2026-01-02T10:00:00+00:00",
        },
    ]
    transport = MockTransport(_notes_body(notes))
    client = _client(transport)
    result = await client.get_lead_notes(_scope(), 7)
    assert result[0].author is None


async def test_header_carries_the_requesting_tenants_key():
    # THE isolation test for F2: two tenants, two different keys, never mixed.
    transport = MockTransport(_list_body([{"id": 1}]))
    client = _client(transport)

    await client.get_leads(_scope("tenant-a"))
    assert transport.last_headers == {"DD-API-KEY": "key-tenant-a"}

    await client.get_leads(_scope("acme"))
    assert transport.last_headers == {"DD-API-KEY": "key-acme"}


async def test_unknown_tenant_fails_closed_before_any_network_call():
    transport = MockTransport(_list_body([{"id": 1}]))
    client = _client(transport)
    with pytest.raises(BackendKeyError):
        await client.get_leads(_scope("ghost"))
    assert transport.calls == 0


async def test_missing_key_is_not_wrapped_by_the_watchdog():
    transport = MockTransport(_list_body([{"id": 1}]))
    client = _client(transport)
    try:
        await client.get_leads(_scope("ghost"))
    except BackendKeyError:
        pass
    except ExternalCallError:
        pytest.fail("BackendKeyError must not be wrapped as ExternalCallError")
    else:
        pytest.fail("expected BackendKeyError")


def _request_on(state: object) -> Request:
    """A bare request whose app carries `state`, as the lifespan leaves it."""
    return Request({"type": "http", "app": SimpleNamespace(state=state)})


async def test_the_factory_wraps_the_lifespan_crm_client():
    """Register item 4: every request's client shares the ONE pooled AsyncClient."""
    async with httpx.AsyncClient() as http:
        state = SimpleNamespace(crm_http=http)
        first = get_leads_client(_request_on(state))
        second = get_leads_client(_request_on(state))
    assert isinstance(first, LeadsClient)
    assert first._transport._client is http  # type: ignore[attr-defined]
    assert second._transport._client is http  # type: ignore[attr-defined]


def test_the_factory_refuses_without_a_lifespan_client():
    """No pooled client on app.state is 503 backend_unavailable, never a new pool."""
    with pytest.raises(BackendUnavailableError):
        get_leads_client(_request_on(SimpleNamespace()))


def test_the_transport_holds_no_number_of_its_own():
    """The timeout and the limits live on the lifespan client, from Settings."""
    source = pathlib.Path("src/dodeal_ai/tools/httpx_transport.py")
    literals = [
        node.value
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]
    assert literals == []


# --- DODEAL_BACKEND_SCHEME (register item 78, demo only) --------------------


async def test_the_default_scheme_is_https_and_no_env_is_needed_to_get_it():
    """An unset DODEAL_BACKEND_SCHEME still builds an https URL."""
    settings = Settings(
        _env_file=None,
        jwt_signing_key="test-key",
        dd_api_keys={"tenant-a": "key-tenant-a"},
        backend_base_domain="dodealcrm.com",
    )
    assert settings.backend_scheme == "https"
    transport = MockTransport(_list_body([{"id": 1}]))
    await _client(transport, settings).get_leads(_scope("tenant-a"))
    assert transport.last_url == "https://tenant-a.dodealcrm.com/api/service/leads"


async def test_http_is_produced_only_when_the_setting_says_http():
    """The demo scheme reaches the URL, and reaches only the scheme."""
    settings = _settings().model_copy(update={"backend_scheme": "http"})
    transport = MockTransport(_list_body([{"id": 1}]))
    await _client(transport, settings).get_leads(_scope("tenant-a"))
    assert transport.last_url == "http://tenant-a.dodealcrm.com/api/service/leads"


async def test_the_scheme_applies_to_every_read_path():
    """get_lead and get_lead_notes build their URLs through the same seam."""
    settings = _settings().model_copy(update={"backend_scheme": "http"})
    transport = MockTransport(_lead_body({"id": 7}))
    await _client(transport, settings).get_lead(_scope("tenant-a"), 7)
    assert transport.last_url == "http://tenant-a.dodealcrm.com/api/service/leads/7"

    transport = MockTransport(_notes_body([]))
    await _client(transport, settings).get_lead_notes(_scope("tenant-a"), 7)
    assert (
        transport.last_url == "http://tenant-a.dodealcrm.com/api/service/leads/7/notes"
    )


def test_a_scheme_that_is_neither_https_nor_http_is_refused_at_construction():
    """ftp:// or a typo fails closed when Settings is built, not at the call."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            jwt_signing_key="test-key",
            backend_scheme="ftp",
        )


# --- row-by-row validation (register item 90) --------------------------------

SENTINEL = "SENTINEL-row-value-9f3c"


def _note_row(note_id: int, **overrides: object) -> dict:
    row = {
        "id": note_id,
        "note": f"Note {note_id}.",
        "author": None,
        "author_id": 10,
        "createdAt": "2026-01-01T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def _rejected_rows(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "backend_rejected_rows"]


async def test_a_bad_note_row_is_dropped_and_the_rest_kept_in_order(caplog):
    """One invalid row costs that row: the others come back in page order."""
    rows = [_note_row(1), _note_row(2, author_id=SENTINEL), _note_row(3)]
    transport = MockTransport(_notes_body(rows))
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.tools"):
        notes = await _client(transport).get_lead_notes(_scope(), 7)
    assert [n.id for n in notes] == [1, 3]
    (record,) = _rejected_rows(caplog)
    assert record.count == 1
    assert record.endpoint == "tool.get_lead_notes"


async def test_a_bad_lead_row_is_dropped_and_counted(caplog):
    """The lead list is validated per row the same way."""
    rows = [{"id": 1}, {"id": SENTINEL}, {"name": "no id"}, {"id": 4}]
    transport = MockTransport(_list_body(rows))
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.tools"):
        leads = await _client(transport).get_leads(_scope())
    assert [lead.id for lead in leads] == [1, 4]
    (record,) = _rejected_rows(caplog)
    assert record.count == 2
    assert record.endpoint == "tool.get_leads"


async def test_the_dropped_row_line_carries_no_value(caplog):
    """No field of a rejected row reaches any log record."""
    rows = [_note_row(1, note=SENTINEL, author_id=SENTINEL)]
    transport = MockTransport(_notes_body(rows))
    with caplog.at_level(logging.DEBUG):
        assert await _client(transport).get_lead_notes(_scope(), 7) == []
    assert SENTINEL not in caplog.text
    assert all(SENTINEL not in str(r.__dict__) for r in caplog.records)


async def test_a_clean_page_logs_nothing(caplog):
    """No dropped rows, no line."""
    transport = MockTransport(_notes_body([_note_row(1)]))
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.tools"):
        await _client(transport).get_lead_notes(_scope(), 7)
    assert _rejected_rows(caplog) == []


async def test_a_malformed_envelope_still_fails_closed():
    """Row tolerance does not extend to the envelope: no data list is an error."""
    transport = MockTransport({"status": True, "data": "not-a-list", "meta": _meta(0)})
    with pytest.raises(OutputValidationError):
        await _client(transport).get_lead_notes(_scope(), 7)


async def test_a_string_booked_amount_is_accepted():
    """bookedAmount is Any: a formatted string no longer rejects the lead."""
    transport = MockTransport(_lead_body({"id": 7, "bookedAmount": "1,250,000"}))
    lead = await _client(transport).get_lead(_scope(), 7)
    assert lead.bookedAmount == "1,250,000"


async def test_the_requested_notes_invalid_row_is_note_not_found():
    """Through the pipeline: the requested note's bad row is 404, nothing reserved."""
    rows = [_note_row(10, createdAt=None), _note_row(11)]

    class _Pages:
        calls = 0

        async def get_json(self, url: str, headers: dict[str, str]) -> object:
            self.calls += 1
            if url.endswith("/notes"):
                return _notes_body(rows)
            return _lead_body({"id": 7})

    llm = FakeLLM()
    deps = JudgementDeps(
        leads=_client(_Pages()),  # type: ignore[arg-type]
        llm=llm,
        config=get_tenant_config("tenant-a"),
        settings=_settings(),
    )
    with pytest.raises(NoteNotFoundError):
        await judge_note(
            _scope(),
            JudgementRequest(lead_id=7, note_id=10),
            resubmission=False,
            deps=deps,
        )
    assert llm.call_count == 0
