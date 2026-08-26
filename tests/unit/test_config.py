"""Config layer: defaults, env overrides, and fail-closed behaviour."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

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


def test_claim_name_defaults_are_the_confirmed_names(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    s = get_settings()
    assert s.claim_subject == "sub"
    assert s.claim_subdomain == "subdomain"
    assert s.claim_database == "database"


def test_env_overrides_apply(monkeypatch):
    # A future rename (or RS256) is a config change, not a code edit.
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    monkeypatch.setenv("DODEAL_JWT_ALGORITHM", "RS256")
    monkeypatch.setenv("DODEAL_CLAIM_SUBJECT", "user_id")
    monkeypatch.setenv("DODEAL_CLAIM_SUBDOMAIN", "tenant_sub")
    monkeypatch.setenv("DODEAL_CLAIM_DATABASE", "db")
    s = get_settings()
    assert s.jwt_algorithm == "RS256"
    assert s.claim_subject == "user_id"
    assert s.claim_subdomain == "tenant_sub"
    assert s.claim_database == "db"


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


def test_dd_api_keys_parses_json_map_of_secrets(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    monkeypatch.setenv("DODEAL_DD_API_KEYS", '{"t1":"k1","t2":"k2"}')
    s = get_settings()
    assert set(s.dd_api_keys) == {"t1", "t2"}
    assert isinstance(s.dd_api_keys["t1"], SecretStr)
    assert s.dd_api_keys["t1"].get_secret_value() == "k1"
    assert s.dd_api_keys["t2"].get_secret_value() == "k2"
    assert "k1" not in repr(s)
    assert "k1" not in str(s)
    assert "k2" not in repr(s)
    assert "k2" not in str(s)
