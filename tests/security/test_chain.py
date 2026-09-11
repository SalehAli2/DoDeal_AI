"""Gate chain over HTTP: auth + tenancy (Gate 3 parked)."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import dodeal_ai.core.auth.dependencies as auth_dependencies
import dodeal_ai.core.cost.limiter as cost_limiter
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.cost.limiter import CostLimitError
from dodeal_ai.main import app
from tests.helpers import tokens
from tests.helpers.fake_cost_redis import FakeCostRedis


@pytest.fixture(autouse=True)
def _fake_cost(monkeypatch):
    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: FakeCostRedis())


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
    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get("/_probe/protected", headers={**_auth(token), **_host("tenant-a")})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == "tenant-a"
    assert body["subject"] == "42"
    assert body["database"] == "crm_tenant_a"


def test_missing_auth_header_401(client):
    r = client.get("/_probe/protected", headers=_host("tenant-a"))
    assert r.status_code == 401


def test_bad_token_401(client):
    r = client.get(
        "/_probe/protected", headers={**_auth("not-a-token"), **_host("tenant-a")}
    )
    assert r.status_code == 401


def test_cross_tenant_host_403(client):
    # Token subdomain tenant-a, Host subdomain other -> Gate 2 denies.
    token = tokens.mint_token(subdomain="tenant-a")
    r = client.get("/_probe/protected", headers={**_auth(token), **_host("other")})
    assert r.status_code == 403


def test_missing_host_subdomain_403(client):
    token = tokens.mint_token(subdomain="tenant-a")
    r = client.get(
        "/_probe/protected", headers={**_auth(token), "Host": "dodealcrm.com"}
    )
    assert r.status_code == 403


def test_request_id_is_real_not_unknown(client, json_log):
    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get("/_probe/protected", headers={**_auth(token), **_host("tenant-a")})
    assert r.status_code == 200
    request_ids = {
        line["request_id"] for line in json_log() if line["logger"] == "dodeal_ai.audit"
    }
    assert "unknown" not in request_ids
    assert all(request_ids)


def test_incoming_x_request_id_header_is_honored(client):
    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get(
        "/_probe/protected",
        headers={
            **_auth(token),
            **_host("tenant-a"),
            "X-Request-ID": "caller-supplied-id",
        },
    )
    assert r.status_code == 200
    assert r.json()["request_id"] == "caller-supplied-id"
    assert r.headers["X-Request-ID"] == "caller-supplied-id"


def _assert_is_generated_uuid4(value: str) -> None:
    """The middleware fell back to generating an id, i.e. it did not take the
    caller's."""
    assert uuid.UUID(value).version == 4


def test_overlong_inbound_request_id_is_replaced_by_a_generated_uuid(client, json_log):
    # M6: the inbound id reaches every audit line and the response header. A
    # 200-char value is over the 128 cap, so it is discarded as if absent.
    overlong = "a" * 200
    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get(
        "/_probe/protected",
        headers={**_auth(token), **_host("tenant-a"), "X-Request-ID": overlong},
    )

    assert r.status_code == 200
    assert r.headers["X-Request-ID"] != overlong
    _assert_is_generated_uuid4(r.headers["X-Request-ID"])
    assert r.json()["request_id"] == r.headers["X-Request-ID"]
    # The rejected value is never logged -- not even to say it was rejected.
    assert overlong not in json.dumps(json_log())


@pytest.mark.parametrize(
    ("label", "forged"),
    [
        ("newline", "req-1\nlots of forged log line"),
        ("crlf", "req-1\r\nX-Evil: 1"),
        ("json_brace", '{"decision":"allow","gate":"auth"}'),
    ],
)
def test_malformed_inbound_request_id_is_replaced_by_a_generated_uuid(
    client, json_log, label, forged
):
    # A newline would forge a SECOND record in the log stream; a leading "{"
    # is what an earlier JsonFormatter bug parsed as structured fields. Both
    # are discarded before the id reaches a log line or the response.
    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get(
        "/_probe/protected",
        headers={**_auth(token), **_host("tenant-a"), "X-Request-ID": forged},
    )

    assert r.status_code == 200
    assert r.headers["X-Request-ID"] != forged
    _assert_is_generated_uuid4(r.headers["X-Request-ID"])
    # json_log() parses every captured line: it raises if a forged newline
    # split one record into two. Nothing of the rejected value survives.
    assert forged not in json.dumps(json_log())


def test_response_carries_generated_request_id_header(client):
    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get("/_probe/protected", headers={**_auth(token), **_host("tenant-a")})
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] == r.json()["request_id"]


def test_mixed_case_host_is_now_allowed(client):
    # H4: Gate 2 compares case-insensitively -- this used to be a 403.
    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get(
        "/_probe/protected",
        headers={**_auth(token), "Host": "Tenant-A.dodealcrm.com"},
    )
    assert r.status_code == 200
    assert r.json()["tenant"] == "tenant-a"


def test_cross_domain_host_denied_with_invalid_host(client, json_log):
    # H4: only the first label used to be checked, so "tenant-a.evil.com"
    # passed Gate 2 for a token belonging to tenant-a. Now denied outright.
    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get(
        "/_probe/protected",
        headers={**_auth(token), "Host": "tenant-a.evil.com"},
    )
    assert r.status_code == 403

    deny_lines = [line for line in json_log() if line["logger"] == "dodeal_ai.audit"]
    assert any(
        line["gate"] == "tenancy" and line["reason_code"] == "invalid_host"
        for line in deny_lines
    )


def test_cost_cap_exceeded_is_generic_429_and_audited(client, monkeypatch, json_log):
    def _raise(*args, **kwargs):
        raise CostLimitError("tenant_quota_exceeded")

    monkeypatch.setattr(auth_dependencies, "enforce_cost", _raise)

    token = tokens.mint_token(subdomain="tenant-a", sub=42)
    r = client.get("/_probe/protected", headers={**_auth(token), **_host("tenant-a")})

    assert r.status_code == 429
    assert r.json() == {"detail": "Too Many Requests"}
    assert "tenant_quota_exceeded" not in r.text

    deny_lines = [line for line in json_log() if line["logger"] == "dodeal_ai.audit"]
    assert any(
        line["gate"] == "cost" and line["reason_code"] == "tenant_quota_exceeded"
        for line in deny_lines
    )
