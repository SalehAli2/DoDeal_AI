"""Phase 0 exit-demo (confirmed world): cross-tenant blocked+logged, bad token
rejected, happy path passes auth+tenancy, alg:none rejected."""
from __future__ import annotations

import json

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
        _env_file=None, jwt_signing_key=tokens.TEST_SECRET, jwt_algorithm=tokens.TEST_ALG
    )
    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _host(subdomain: str) -> dict:
    return {"Host": f"{subdomain}.dodealcrm.com"}


def test_criterion_1_cross_tenant_blocked_and_logged(client, caplog):
    token = tokens.mint_token(subdomain="nasir3")
    with caplog.at_level("WARNING", logger="dodeal_ai.audit"):
        r = client.get("/_probe/protected", headers={**_auth(token), **_host("other")})
    assert r.status_code == 403
    denies = [json.loads(rec.message) for rec in caplog.records
              if rec.levelname == "WARNING"]
    assert any(d["decision"] == "deny" and d["gate"] == "tenancy"
               and d["reason_code"] == "tenant_mismatch" for d in denies)


def test_criterion_2_expired_token_rejected(client):
    r = client.get("/_probe/protected",
                   headers={**_auth(tokens.mint_expired_token()), **_host("nasir3")})
    assert r.status_code == 401


def test_criterion_2_malformed_token_rejected(client):
    r = client.get("/_probe/protected",
                   headers={**_auth("garbage.not.jwt"), **_host("nasir3")})
    assert r.status_code == 401


def test_criterion_3_valid_token_passes_auth_and_tenancy(client, caplog):
    token = tokens.mint_token(subdomain="nasir3", sub=42)
    with caplog.at_level("INFO", logger="dodeal_ai.audit"):
        r = client.get("/_probe/protected", headers={**_auth(token), **_host("nasir3")})
    assert r.status_code == 200
    allow_gates = {json.loads(rec.message)["gate"] for rec in caplog.records
                   if json.loads(rec.message)["decision"] == "allow"}
    assert {"auth", "tenancy"} <= allow_gates


def test_criterion_4_alg_none_rejected(client):
    r = client.get("/_probe/protected",
                   headers={**_auth(tokens.mint_alg_none_token()), **_host("nasir3")})
    assert r.status_code == 401