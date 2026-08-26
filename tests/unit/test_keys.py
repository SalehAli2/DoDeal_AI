"""TenantKeyResolver / SettingsKeyResolver: per-tenant backend key lookup,
failing closed on an unknown tenant with no key material logged (F2)."""

from __future__ import annotations

from pydantic import SecretStr

from dodeal_ai.core.config import Settings
from dodeal_ai.tools.keys import BackendKeyError, SettingsKeyResolver, TenantKeyResolver


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key="test-key",
        dd_api_keys={"nasir3": "key-nasir3", "acme": "key-acme"},
    )


def test_known_tenant_resolves_to_its_configured_secret():
    resolver = SettingsKeyResolver(_settings())
    resolved = resolver.resolve("nasir3")
    assert isinstance(resolved, SecretStr)
    assert resolved.get_secret_value() == "key-nasir3"


def test_unknown_tenant_fails_closed_with_no_key_material(caplog):
    resolver = SettingsKeyResolver(_settings())

    with caplog.at_level("ERROR", logger="dodeal_ai.tools"):
        try:
            resolver.resolve("ghost")
            raised = None
        except BackendKeyError as exc:
            raised = exc

    assert raised is not None
    assert raised.reason_code == "backend_key_missing"
    assert str(raised) == "backend_key_missing"
    assert "key-nasir3" not in str(raised)
    assert "key-acme" not in str(raised)

    assert "ghost" in caplog.text
    assert "key-nasir3" not in caplog.text
    assert "key-acme" not in caplog.text


def test_settings_key_resolver_satisfies_the_protocol():
    # TenantKeyResolver is @runtime_checkable, so conformance is provable with
    # isinstance() directly here -- no need for a typed assignment in
    # tests/helpers/ just to get mypy to check it.
    assert isinstance(SettingsKeyResolver(_settings()), TenantKeyResolver)
