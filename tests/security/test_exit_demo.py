"""Phase 0 exit-demo — the four criteria, proven in one readable file.

This is the demo script. Each test maps to one exit criterion from the spec:
  1. Cross-tenant request blocked AND an audit deny line emitted.
  2. Bad token (malformed / expired / wrong-aud) rejected.
  3. Happy path: valid tenant-A token passes all gates, context is correct.
  4. alg:none token rejected.

Everything runs against the self-generated TEST key (no real secret, no
network). The tool call ("returns only tenant A's data") is NOT part of this
demo — get_lead is blocked on backend Q4 and is deliberately out of scope today.
"""
from __future__ import annotations

import json

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


# --- Criterion 1: cross-tenant blocked AND logged ---------------------------

def test_criterion_1_cross_tenant_blocked_and_logged(client, caplog):
    # Tenant B's header reaching past a tenant-A token -> 403 at Gate 2,
    # with a structured deny line naming the tenancy gate.
    token = tokens.mint_token(tenant_id="tenant-a")
    with caplog.at_level("WARNING", logger="dodeal_ai.audit"):
        r = client.get(
            "/_probe/protected",
            headers={**_auth(token), "X-Tenant-ID": "tenant-b"},
        )
    assert r.status_code == 403

    deny_lines = [
        json.loads(rec.message)
        for rec in caplog.records
        if rec.levelname == "WARNING"
    ]
    assert any(
        d["decision"] == "deny"
        and d["gate"] == "tenancy"
        and d["reason_code"] == "tenant_mismatch"
        and d["tenant_id"] == "tenant-a"
        for d in deny_lines
    ), "expected a tenancy deny line for the authoritative tenant"


# --- Criterion 2: bad tokens rejected ---------------------------------------

def test_criterion_2_expired_token_rejected(client):
    r = client.get("/_probe/protected", headers=_auth(tokens.mint_expired_token()))
    assert r.status_code == 401


def test_criterion_2_wrong_audience_rejected(client):
    r = client.get("/_probe/protected", headers=_auth(tokens.mint_wrong_aud_token()))
    assert r.status_code == 401


def test_criterion_2_malformed_token_rejected(client):
    r = client.get("/_probe/protected", headers=_auth("garbage.not.a.jwt"))
    assert r.status_code == 401


# --- Criterion 3: happy path ------------------------------------------------

def test_criterion_3_valid_tenant_a_passes_all_gates(client, caplog):
    token = tokens.mint_token(tenant_id="tenant-a", sub="user-1")  # agent role
    with caplog.at_level("INFO", logger="dodeal_ai.audit"):
        r = client.get("/_probe/protected", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["subject"] == "user-1"
    assert body["roles"] == ["agent"]

    # All three gates allowed, each emitting an allow line.
    allow_gates = {
        json.loads(rec.message)["gate"]
        for rec in caplog.records
        if json.loads(rec.message)["decision"] == "allow"
    }
    assert {"auth", "tenancy", "authz"} <= allow_gates


# --- Criterion 4: alg:none rejected -----------------------------------------

def test_criterion_4_alg_none_rejected(client):
    r = client.get("/_probe/protected", headers=_auth(tokens.mint_alg_none_token()))
    assert r.status_code == 401