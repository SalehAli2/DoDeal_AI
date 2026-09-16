"""Config layer: defaults, env overrides, and fail-closed behaviour."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from dodeal_ai.core.config import ConfigError, get_settings


def test_required_key_present_yields_defaults(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    s = get_settings()
    assert s.jwt_signing_key.get_secret_value() == "test-key-abc"
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

# (env var, field, default, the type a set value parses to). The pool size's
# default is None -- unset means "derived from max_inflight" (redis_pool_size) --
# so its type cannot be read off the default the way the three timeouts' can.
_REDIS_BUDGET: tuple[tuple[str, str, object, type], ...] = (
    (
        "DODEAL_REDIS_CONNECT_TIMEOUT_SECONDS",
        "redis_connect_timeout_seconds",
        0.25,
        float,
    ),
    ("DODEAL_REDIS_SOCKET_TIMEOUT_SECONDS", "redis_socket_timeout_seconds", 1.0, float),
    ("DODEAL_REDIS_MAX_CONNECTIONS", "redis_max_connections", None, int),
    (
        "DODEAL_REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS",
        "redis_pool_acquire_timeout_seconds",
        1.0,
        float,
    ),
)


@pytest.mark.parametrize(("env_name", "field", "default", "kind"), _REDIS_BUDGET)
def test_the_redis_budget_defaults_are_the_recorded_values(
    env_name, field, default, kind
):
    """The four provisional values a deployment inherits if it sets nothing."""
    from dodeal_ai.core.config import Settings

    assert getattr(Settings(_env_file=None, jwt_signing_key="k"), field) == default


@pytest.mark.parametrize(("env_name", "field", "default", "kind"), _REDIS_BUDGET)
def test_the_redis_budget_round_trips_through_the_environment(
    monkeypatch, env_name, field, default, kind
):
    """Sizing Redis must be a deployment change, not a code change."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    monkeypatch.setenv(env_name, "5")
    get_settings.cache_clear()

    assert getattr(get_settings(), field) == kind(5)


@pytest.mark.parametrize(("env_name", "field", "default", "kind"), _REDIS_BUDGET)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_redis_budget_is_refused_at_construction(
    monkeypatch, env_name, field, default, kind, value
):
    """Zero is not "no limit": a zero timeout can never succeed and a zero pool
    can never hand out a connection, so both read as an outage while being a
    typo. They fail closed at settings load, like a missing signing key."""
    from dodeal_ai.core.config import _build_settings

    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key-abc")
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ConfigError):
        _build_settings(_env_file=None)


# --- the judgement deadline (register item 83) ------------------------------


def test_the_judgement_deadline_is_the_recorded_provisional_value(monkeypatch):
    """25 s until Q16 says what the CRM itself waits, and settable without code."""
    from dodeal_ai.core.config import Settings

    assert (
        Settings(_env_file=None, jwt_signing_key="k").judgement_deadline_seconds == 25.0
    )

    monkeypatch.setenv("DODEAL_JUDGEMENT_DEADLINE_SECONDS", "12.5")
    get_settings.cache_clear()
    assert get_settings().judgement_deadline_seconds == 12.5


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_judgement_deadline_is_refused(monkeypatch, value):
    """A zero deadline would 503 every judgement before its first await: an
    outage that is really a typo, so it fails at settings load instead."""
    from dodeal_ai.core.config import _build_settings

    monkeypatch.setenv("DODEAL_JUDGEMENT_DEADLINE_SECONDS", value)
    with pytest.raises(ConfigError):
        _build_settings(_env_file=None)


# --- the signing key is a secret (register item 91) --------------------------

# Bound to a NAME and only ever passed by name, so a traceback that quotes a test's
# source line can never carry the value.
SIGNING_KEY_SENTINEL = "SENTINEL-signing-key-7f3a9c"


def test_the_signing_key_is_a_secret_absent_from_repr_and_str():
    """The key is a SecretStr that repr and str both mask, and it still reads back."""
    from dodeal_ai.core.config import Settings

    settings = Settings(_env_file=None, jwt_signing_key=SIGNING_KEY_SENTINEL)

    assert SIGNING_KEY_SENTINEL not in repr(settings)
    assert SIGNING_KEY_SENTINEL not in str(settings)
    assert isinstance(settings.jwt_signing_key, SecretStr)
    assert settings.jwt_signing_key.get_secret_value() == SIGNING_KEY_SENTINEL


def test_a_missing_signing_key_still_raises_config_error_unchained(monkeypatch):
    """No key is still a ConfigError, raised from None so no validation error rides on it."""
    from dodeal_ai.core.config import _build_settings

    monkeypatch.delenv("DODEAL_JWT_SIGNING_KEY", raising=False)
    with pytest.raises(ConfigError) as raised:
        _build_settings(_env_file=None)

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_a_config_error_carries_no_signing_key_value():
    """A rejected signing key reaches neither the ConfigError's text nor its formatted traceback."""
    import traceback

    from dodeal_ai.core.config import _build_settings

    # A list is not a string, so pydantic refuses it and quotes the input it saw.
    with pytest.raises(ConfigError) as raised:
        _build_settings(_env_file=None, jwt_signing_key=[SIGNING_KEY_SENTINEL])

    formatted = "".join(traceback.format_exception(raised.value))
    assert SIGNING_KEY_SENTINEL not in str(raised.value)
    assert SIGNING_KEY_SENTINEL not in formatted
    assert "ValidationError" not in formatted


def test_the_signing_key_is_read_in_exactly_one_place_in_src():
    """Every `jwt_signing_key.get_secret_value()` call under src/ is the one in verify.py."""
    import ast
    import pathlib

    reads = []
    for path in sorted(pathlib.Path("src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_secret_value"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "jwt_signing_key"
            ):
                reads.append(path.as_posix())

    assert reads == ["src/dodeal_ai/core/auth/verify.py"]
