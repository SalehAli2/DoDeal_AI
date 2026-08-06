"""Global error handler: unexpected errors fail closed; deny paths untouched."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

from dodeal_ai.main import app

# A throwaway router that deliberately raises, mounted only for these tests.
_boom = APIRouter(prefix="/_boom", tags=["test-only"])


@_boom.get("/unexpected")
def _unexpected():
    raise RuntimeError("this should never leak to the client")


@_boom.get("/deliberate-403")
def _deliberate_403():
    raise HTTPException(status_code=403, detail="Forbidden")


@pytest.fixture
def client():
    app.include_router(_boom)
    yield TestClient(app, raise_server_exceptions=False)
    # remove the test router so it doesn't leak into other tests
    app.router.routes = [
        r
        for r in app.router.routes
        if getattr(r, "path", "").startswith("/_boom") is False
    ]


def test_unexpected_error_returns_generic_500(client):
    r = client.get("/_boom/unexpected")
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Internal Server Error"
    assert "request_id" in body


def test_unexpected_error_leaks_no_internal_detail(client):
    r = client.get("/_boom/unexpected")
    text = r.text
    # The exception message and type must NOT appear in the response.
    assert "this should never leak" not in text
    assert "RuntimeError" not in text
    assert "Traceback" not in text


def test_unexpected_error_logged_at_error_with_request_id(client, caplog):
    with caplog.at_level("ERROR", logger="dodeal_ai.error"):
        client.get("/_boom/unexpected")
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, "expected an ERROR log line for the unhandled exception"
    assert "unhandled_exception" in error_records[-1].getMessage()


def test_deliberate_http_error_is_not_swallowed(client):
    # A real deny path must still return its own status, NOT a generic 500.
    r = client.get("/_boom/deliberate-403")
    assert r.status_code == 403
    assert r.json()["detail"] == "Forbidden"
