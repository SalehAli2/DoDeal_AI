"""Config layer: defaults, env overrides, and fail-closed behaviour."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dodeal_ai.core.config import ConfigError, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_required_key_present_yields_defaults(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    s = get_settings()
    assert s.jwt_signing_key == "test-key-abc"
    assert s.jwt_algorithm == "HS256"
    assert s.jwt_issuer == "hikal-test-issuer"
    assert s.jwt_audience == "dodeal-ai-test"


def test_claim_name_defaults_are_the_assumed_names(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    s = get_settings()
    assert s.claim_tenant_id == "tenant_id"
    assert s.claim_subject == "sub"
    assert s.claim_roles == "roles"


def test_env_overrides_apply(monkeypatch):
    # Simulates the backend confirming org_id / user_id / role + RS256:
    # one env change, no code edits.
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    monkeypatch.setenv("DODEAL_JWT_ALGORITHM", "RS256")
    monkeypatch.setenv("DODEAL_CLAIM_TENANT_ID", "org_id")
    monkeypatch.setenv("DODEAL_CLAIM_SUBJECT", "user_id")
    monkeypatch.setenv("DODEAL_CLAIM_ROLES", "role")
    s = get_settings()
    assert s.jwt_algorithm == "RS256"
    assert s.claim_tenant_id == "org_id"
    assert s.claim_subject == "user_id"
    assert s.claim_roles == "role"


def test_settings_are_frozen(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    s = get_settings()
    with pytest.raises(ValidationError):
        s.jwt_algorithm = "none"


def test_missing_key_fails_closed(monkeypatch):
    # _env_file=None makes this deterministic: a local .env can't accidentally
    # supply the key and mask the fail-closed path.
    from dodeal_ai.core.config import _build_settings

    monkeypatch.delenv("DODEAL_JWT_SIGNING_KEY", raising=False)
    with pytest.raises(ConfigError):
        _build_settings(_env_file=None)