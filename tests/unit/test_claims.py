"""Claim mapping: confirmed Tymon claims (sub int / subdomain / database),
no roles from token, integer sub normalised, swap stays config-only."""

from __future__ import annotations

import pytest

from dodeal_ai.core.auth.claims import (
    ClaimMappingError,
    Identity,
    extract_identity,
)
from dodeal_ai.core.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "jwt_signing_key": "test-key-abc",
        "claim_subject": "sub",
        "claim_subdomain": "subdomain",
        "claim_database": "database",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_extracts_confirmed_identity():
    payload = {"sub": 42, "subdomain": "nasir3", "database": "crm_nasir3"}
    ident = extract_identity(payload, _settings())
    assert ident == Identity(
        tenant="nasir3", subject="42", database="crm_nasir3", roles=()
    )


def test_integer_sub_normalised_to_string():
    ident = extract_identity(
        {"sub": 42, "subdomain": "nasir3", "database": "crm_nasir3"}, _settings()
    )
    assert ident.subject == "42"
    assert isinstance(ident.subject, str)


def test_string_sub_also_accepted():
    ident = extract_identity(
        {"sub": "42", "subdomain": "nasir3", "database": "crm_nasir3"}, _settings()
    )
    assert ident.subject == "42"


def test_bool_sub_rejected():
    # bool is an int subclass; must not slip through as "True".
    with pytest.raises(ClaimMappingError) as exc:
        extract_identity(
            {"sub": True, "subdomain": "nasir3", "database": "crm_nasir3"}, _settings()
        )
    assert exc.value.reason_code == "missing_subject"


def test_roles_never_sourced_from_token():
    payload = {
        "sub": 42,
        "subdomain": "nasir3",
        "database": "crm_nasir3",
        "roles": ["agent"],
    }
    ident = extract_identity(payload, _settings())
    assert ident.roles == ()


def test_missing_subdomain_raises():
    with pytest.raises(ClaimMappingError) as exc:
        extract_identity({"sub": 42, "database": "crm_nasir3"}, _settings())
    assert exc.value.reason_code == "missing_subdomain"


def test_missing_subject_raises():
    with pytest.raises(ClaimMappingError) as exc:
        extract_identity({"subdomain": "nasir3", "database": "crm_nasir3"}, _settings())
    assert exc.value.reason_code == "missing_subject"


def test_absent_database_defaults_empty_not_error():
    ident = extract_identity({"sub": 42, "subdomain": "nasir3"}, _settings())
    assert ident.database == ""


def test_swapping_claim_names_is_config_only():
    payload = {"user_id": 9, "tenant_sub": "beta", "db": "beta_db"}
    settings = _settings(
        claim_subject="user_id", claim_subdomain="tenant_sub", claim_database="db"
    )
    ident = extract_identity(payload, settings)
    assert ident == Identity(tenant="beta", subject="9", database="beta_db", roles=())


def test_tenant_claim_is_lowercased():
    ident = extract_identity(
        {"sub": 1, "subdomain": "Tenant-A", "database": "d"}, _settings()
    )
    assert ident.tenant == "tenant-a"


@pytest.mark.parametrize(
    "value",
    [
        "abc@evil",
        "x y",
        "a..b",
        "-",
        "-abc",
        "abc-",
        "a" * 64,
        "tenant-a.dodealcrm.com",
    ],
)
def test_invalid_tenant_claim_rejected(value):
    with pytest.raises(ClaimMappingError) as exc:
        extract_identity({"sub": 1, "subdomain": value, "database": "d"}, _settings())
    assert exc.value.reason_code == "invalid_tenant_claim"
    assert str(exc.value) == "invalid_tenant_claim"
    assert value not in str(exc.value)


def test_empty_tenant_claim_is_missing_not_invalid():
    with pytest.raises(ClaimMappingError) as exc:
        extract_identity({"sub": 1, "subdomain": "", "database": "d"}, _settings())
    assert exc.value.reason_code == "missing_subdomain"
    assert str(exc.value) == "missing_subdomain"
