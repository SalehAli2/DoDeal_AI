"""Config layer: defaults, env overrides, and fail-closed behaviour."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from dodeal_ai.core.config import ConfigError, get_settings


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


# --- Redis connection budget (audit H3) -------------------------------------

_REDIS_BUDGET: tuple[tuple[str, str, object], ...] = (
    ("DODEAL_REDIS_CONNECT_TIMEOUT_SECONDS", "redis_connect_timeout_seconds", 0.25),
    ("DODEAL_REDIS_SOCKET_TIMEOUT_SECONDS", "redis_socket_timeout_seconds", 1.0),
    ("DODEAL_REDIS_MAX_CONNECTIONS", "redis_max_connections", 20),
    (
        "DODEAL_REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS",
        "redis_pool_acquire_timeout_seconds",
        1.0,
    ),
)


@pytest.mark.parametrize(("env_name", "field", "default"), _REDIS_BUDGET)
def test_the_redis_budget_defaults_are_the_recorded_values(env_name, field, default):
    """The four provisional numbers a deployment inherits if it sets nothing."""
    from dodeal_ai.core.config import Settings

    assert getattr(Settings(_env_file=None, jwt_signing_key="k"), field) == default


@pytest.mark.parametrize(("env_name", "field", "default"), _REDIS_BUDGET)
def test_the_redis_budget_round_trips_through_the_environment(
    monkeypatch, env_name, field, default
):
    """Sizing Redis must be a deployment change, not a code change."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    monkeypatch.setenv(env_name, "5")
    get_settings.cache_clear()

    assert getattr(get_settings(), field) == type(default)(5)


@pytest.mark.parametrize(("env_name", "field", "default"), _REDIS_BUDGET)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_redis_budget_is_refused_at_construction(
    monkeypatch, env_name, field, default, value
):
    """Zero is not "no limit": a zero timeout can never succeed and a zero pool
    can never hand out a connection, so both read as an outage while being a
    typo. They fail closed at settings load, like a missing signing key."""
    from dodeal_ai.core.config import _build_settings

    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ConfigError):
        _build_settings(_env_file=None)
