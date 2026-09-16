"""F1: valid JSON in the wrong response shape is 503 backend_unavailable with a
backend_envelope_invalid line and no values, never a 500, on both reads."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core import metrics
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app
from dodeal_ai.tools.errors import BackendEnvelopeInvalid
from dodeal_ai.tools.keys import SettingsKeyResolver
from dodeal_ai.tools.leads import LeadsClient, get_leads_client
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM
from tests.helpers.scopes import TEST_SCOPE

SENTINEL = "SENTINEL-envelope-value-5c2d"
META = {"current_page": 1, "per_page": 25, "total": 1, "last_page": 1}
LEAD_OK = {"status": True, "data": {"id": 7}}
NOTES_OK = {
    "status": True,
    "data": [
        {
            "id": 10,
            "note": "Called the client, discussed the 3BR, following up Tuesday.",
            "author_id": 42,
            "createdAt": "2026-01-02T09:00:00+00:00",
        }
    ],
    "meta": META,
}
# Valid JSON, wrong shape: a top-level list, a wrapper with the wrong key, and
# `data` of the wrong type, each carrying a value that must reach no log line.
BAD_SHAPES = [
    [SENTINEL],
    {"success": True, "posts": {"data": SENTINEL}},
    {"status": True, "data": SENTINEL, "meta": META},
]


class Endpoints:
    """The lead and notes reads, answered per path, counting calls."""

    def __init__(self, lead: object, notes: object) -> None:
        self._lead = lead
        self._notes = notes
        self.calls = 0

    async def get_json(self, url: str, headers: dict[str, str]) -> object:
        self.calls += 1
        return self._notes if url.endswith("/notes") else self._lead


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
        dd_api_keys={"tenant-a": "key-tenant-a"},
    )


def _client(transport: Endpoints) -> LeadsClient:
    settings = _settings()
    return LeadsClient(transport, SettingsKeyResolver(settings), settings)


@pytest.fixture
def json_log():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


# --- the tool layer -----------------------------------------------------------


@pytest.mark.parametrize("shape", BAD_SHAPES, ids=["list", "old-wrapper", "data-type"])
@pytest.mark.parametrize(
    ("method", "args", "label"),
    [
        ("get_lead", (7,), "tool.get_lead"),
        ("get_lead_notes", (7,), "tool.get_lead_notes"),
        ("get_leads", (), "tool.get_leads"),
    ],
    ids=["lead", "notes", "leads"],
)
async def test_a_wrong_shape_is_envelope_invalid_with_one_valueless_line(
    json_log, shape, method, args, label
):
    """Each read raises BackendEnvelopeInvalid once, unretried, and logs no value."""
    transport = Endpoints(lead=shape, notes=shape)

    with pytest.raises(BackendEnvelopeInvalid) as caught:
        await getattr(_client(transport), method)(TEST_SCOPE, *args)

    assert transport.calls == 1
    assert caught.value.label == label
    assert str(caught.value) == "backend_envelope_invalid"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    lines = [x for x in _lines(json_log) if x["message"] == "backend_envelope_invalid"]
    assert len(lines) == 1
    assert (lines[0]["level"], lines[0]["tenant"], lines[0]["label"]) == (
        "WARNING",
        "tenant-a",
        label,
    )
    assert lines[0]["error_count"] >= 1
    assert SENTINEL not in json_log.getvalue()
    assert SENTINEL not in repr(caught.value.errors)


# --- through the route, both endpoints ---------------------------------------


@pytest.fixture
def route(monkeypatch):
    """The judgement route with a real LeadsClient over the scripted endpoints."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    get_settings.cache_clear()
    holder: dict[str, Endpoints] = {}
    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(_settings())
    app.dependency_overrides[get_leads_client] = lambda: _client(holder["endpoints"])
    app.dependency_overrides[get_llm_client] = lambda: FakeLLM()

    def _post(endpoints: Endpoints):
        holder["endpoints"] = endpoints
        token = tokens.mint_token(subdomain="tenant-a", sub=42)
        return TestClient(app, raise_server_exceptions=False).post(
            "/api/v1/notes/judgements",
            json={"lead_id": 7, "note_id": 10},
            headers={
                "Authorization": f"Bearer {token}",
                "Host": "tenant-a.dodealcrm.com",
            },
        )

    yield _post
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "endpoints",
    [
        pytest.param(Endpoints(lead=BAD_SHAPES[1], notes=NOTES_OK), id="lead-endpoint"),
        pytest.param(Endpoints(lead=LEAD_OK, notes=BAD_SHAPES[2]), id="notes-endpoint"),
    ],
)
def test_a_wrong_shape_on_either_endpoint_is_503_never_500(route, json_log, endpoints):
    """The CRM is told backend_unavailable, the line says why, and nothing is quoted."""
    kinds = (
        metrics.REGISTRY.get_sample_value(
            "backend_errors_total", {"kind": "envelope_invalid"}
        )
        or 0.0
    )

    r = route(endpoints)

    assert r.status_code == 503
    assert r.json()["reason"] == "backend_unavailable"
    messages = [x["message"] for x in _lines(json_log)]
    assert "backend_envelope_invalid" in messages
    assert "judgement_backend_unavailable" in messages
    assert "unhandled_exception" not in messages
    assert SENTINEL not in json_log.getvalue() and SENTINEL not in r.text
    assert (
        metrics.REGISTRY.get_sample_value(
            "backend_errors_total", {"kind": "envelope_invalid"}
        )
        == kinds + 1
    )
