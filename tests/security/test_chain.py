"""The gate chain end-to-end over HTTP, via the probe route.

Uses dependency_overrides to point get_verifier at a JwtVerifier bound to
test-aligned Settings, so tokens minted by the helper verify correctly without
a real .env.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings
from dodeal_ai.main import app
from tests.helpers import tokens


@pytest.fixture
def client():
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
        jwt_issuer=tokens.TEST_ISS,
        jwt_audience=tokens.TEST_AUD,
    )
    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_happy_path_200(client):
    token = tokens.mint_token(tenant_id="tenant-a", sub="user-1")  # agent role
    r = client.get("/_probe/protected", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["subject"] == "user-1"
    assert body["roles"] == ["agent"]


def test_missing_auth_header_401(client):
    r = client.get("/_probe/protected")
    assert r.status_code == 401


def test_bad_token_401(client):
    r = client.get("/_probe/protected", headers=_auth("not-a-real-token"))
    assert r.status_code == 401


def test_cross_tenant_header_403(client):
    # Token is tenant-a; header claims tenant-b -> Gate 2 blocks with 403.
    token = tokens.mint_token(tenant_id="tenant-a")
    r = client.get(
        "/_probe/protected",
        headers={**_auth(token), "X-Tenant-ID": "tenant-b"},
    )
    assert r.status_code == 403


def test_insufficient_permission_403(client):
    # A role with no lead:read -> Gate 3 denies. Use an unknown role to get an
    # empty permission set.
    token = tokens.mint_token(roles=["nobody"])
    r = client.get("/_probe/protected", headers=_auth(token))
    assert r.status_code == 403