"""Audit logger: correct levels, no sensitive fields, deny lines always emitted."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.audit.logger import audit
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings
from dodeal_ai.main import app
from tests.helpers import tokens


def test_allow_is_info_deny_is_warning(caplog):
    with caplog.at_level("INFO", logger="dodeal_ai.audit"):
        audit(decision="allow", gate="auth", request_id="r1",
              reason_code="ok", tenant="tenant-a")
        audit(decision="deny", gate="authz", request_id="r2",
              reason_code="permission_denied", tenant="tenant-a")
    levels = {r.levelname for r in caplog.records}
    assert "INFO" in levels
    assert "WARNING" in levels


def test_audit_line_is_valid_json_with_no_secrets(caplog):
    with caplog.at_level("INFO", logger="dodeal_ai.audit"):
        audit(decision="allow", gate="auth", request_id="r1",
              reason_code="ok", tenant="tenant-a")
    payload = json.loads(caplog.records[-1].message)
    assert payload["decision"] == "allow"
    assert payload["tenant"] == "tenant-a"
    assert payload["request_id"] == "r1"
    assert payload["reason_code"] == "ok"
    # No token, claims, or secret fields ever present.
    assert set(payload.keys()) == {
        "event", "decision", "gate", "tenant", "request_id", "reason_code",
    }


@pytest.fixture
def client():
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )
    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    yield TestClient(app)
    app.dependency_overrides.clear()

def test_cross_tenant_deny_emits_warning_line(client, caplog):
    token = tokens.mint_token(subdomain="nasir3")
    with caplog.at_level("WARNING", logger="dodeal_ai.audit"):
        r = client.get(
            "/_probe/protected",
            headers={"Authorization": f"Bearer {token}",
                     "Host": "other.dodealcrm.com"},
        )
    assert r.status_code == 403
    denies = [json.loads(rec.message) for rec in caplog.records
              if rec.levelname == "WARNING"]
    assert any(d["gate"] == "tenancy" and d["decision"] == "deny"
               and d["reason_code"] == "tenant_mismatch" for d in denies)