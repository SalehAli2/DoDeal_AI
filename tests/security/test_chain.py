"""Gate chain over HTTP: auth + tenancy (Gate 3 parked)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dodeal_ai.core.cost.limiter as cost_limiter
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.main import app
from tests.helpers import tokens


class _FakeCostRedis:
    def __init__(self):
        self.store = {}

    def eval(self, script, numkeys, *keys_and_args):
        keys = keys_and_args[:numkeys]
        amount = int(keys_and_args[numkeys])
        counts = []
        for key in keys:
            self.store[key] = self.store.get(key, 0) + amount
            counts.append(self.store[key])
        return counts


@pytest.fixture(autouse=True)
def _fake_cost(monkeypatch):
    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: _FakeCostRedis())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )
    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _host(subdomain: str) -> dict:
    return {"Host": f"{subdomain}.dodealcrm.com"}


def test_happy_path_200(client):
    token = tokens.mint_token(subdomain="nasir3", sub=42)
    r = client.get("/_probe/protected", headers={**_auth(token), **_host("nasir3")})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == "nasir3"
    assert body["subject"] == "42"
    assert body["database"] == "crm_nasir3"


def test_missing_auth_header_401(client):
    r = client.get("/_probe/protected", headers=_host("nasir3"))
    assert r.status_code == 401


def test_bad_token_401(client):
    r = client.get(
        "/_probe/protected", headers={**_auth("not-a-token"), **_host("nasir3")}
    )
    assert r.status_code == 401


def test_cross_tenant_host_403(client):
    # Token subdomain nasir3, Host subdomain other -> Gate 2 denies.
    token = tokens.mint_token(subdomain="nasir3")
    r = client.get("/_probe/protected", headers={**_auth(token), **_host("other")})
    assert r.status_code == 403


def test_missing_host_subdomain_403(client):
    token = tokens.mint_token(subdomain="nasir3")
    r = client.get(
        "/_probe/protected", headers={**_auth(token), "Host": "dodealcrm.com"}
    )
    assert r.status_code == 403
