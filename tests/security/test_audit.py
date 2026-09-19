"""Audit logger: correct levels, no sensitive fields, deny lines always emitted."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.audit.logger import audit
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings
from tests.helpers import tokens
from tests.helpers.probe_app import probe_app

# Register item 93: the served app has no probe route, so these mount it.
app = probe_app()


def test_allow_is_info_deny_is_warning(json_log):
    audit(
        decision="allow",
        gate="auth",
        request_id="r1",
        reason_code="ok",
        tenant="tenant-a",
    )
    audit(
        decision="deny",
        gate="authz",
        request_id="r2",
        reason_code="permission_denied",
        tenant="tenant-a",
    )
    levels = {line["level"] for line in json_log()}
    assert "INFO" in levels
    assert "WARNING" in levels


def test_audit_line_is_valid_json_with_no_secrets(json_log):
    audit(
        decision="allow",
        gate="auth",
        request_id="r1",
        reason_code="ok",
        tenant="tenant-a",
    )
    payload = json_log()[-1]
    assert payload["event"] == "auth_decision"
    assert payload["decision"] == "allow"
    assert payload["gate"] == "auth"
    assert payload["tenant"] == "tenant-a"
    assert payload["request_id"] == "r1"
    assert payload["reason_code"] == "ok"
    # The message is a fixed string, not a serialised copy of the fields --
    # nothing here is a JSON object nested inside the line.
    assert payload["message"] == "auth_decision"
    # No token, claims, or secret fields ever present. Only the formatter's own
    # envelope may accompany the audit field set.
    assert set(payload) - {"timestamp", "level", "logger", "message"} == {
        "event",
        "decision",
        "gate",
        "tenant",
        "request_id",
        "reason_code",
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


def test_cross_tenant_deny_emits_warning_line(client, json_log):
    token = tokens.mint_token(subdomain="tenant-a")
    r = client.get(
        "/_probe/protected",
        headers={"Authorization": f"Bearer {token}", "Host": "other.dodealcrm.com"},
    )
    assert r.status_code == 403
    denies = [
        line
        for line in json_log()
        if line["logger"] == "dodeal_ai.audit" and line["level"] == "WARNING"
    ]
    assert any(
        d["gate"] == "tenancy"
        and d["decision"] == "deny"
        and d["reason_code"] == "tenant_mismatch"
        for d in denies
    )
