"""Claim mapping: reads internal identity through the configured claim names,
and swapping those names is a config-only change."""
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
        "claim_tenant_id": "tenant_id",
        "claim_subject": "sub",
        "claim_roles": "roles",
    }
    base.update(overrides)
    # _env_file=None so a stray local .env can't leak into the mapping under test.
    return Settings(_env_file=None, **base)


def test_extracts_identity_with_assumed_names():
    payload = {"tenant_id": "tenant-a", "sub": "user-1", "roles": ["agent"]}
    ident = extract_identity(payload, _settings())
    assert ident == Identity(tenant_id="tenant-a", subject="user-1", roles=("agent",))


def test_roles_absent_yields_empty_tuple_not_error():
    # Authenticated-but-no-roles is legitimate; Gate 3 decides the deny.
    payload = {"tenant_id": "tenant-a", "sub": "user-1"}
    ident = extract_identity(payload, _settings())
    assert ident.roles == ()


def test_missing_tenant_raises_with_reason_code():
    payload = {"sub": "user-1", "roles": ["agent"]}
    with pytest.raises(ClaimMappingError) as exc:
        extract_identity(payload, _settings())
    assert exc.value.reason_code == "missing_tenant"


def test_missing_subject_raises_with_reason_code():
    payload = {"tenant_id": "tenant-a", "roles": ["agent"]}
    with pytest.raises(ClaimMappingError) as exc:
        extract_identity(payload, _settings())
    assert exc.value.reason_code == "missing_subject"


def test_malformed_roles_shape_raises():
    payload = {"tenant_id": "tenant-a", "sub": "user-1", "roles": "agent"}
    with pytest.raises(ClaimMappingError) as exc:
        extract_identity(payload, _settings())
    assert exc.value.reason_code == "malformed_roles"


def test_swapping_claim_names_is_config_only():
    # Backend confirms org_id / user_id / role -> change config, not code.
    payload = {"org_id": "tenant-b", "user_id": "user-9", "role": ["viewer"]}
    settings = _settings(
        claim_tenant_id="org_id", claim_subject="user_id", claim_roles="role"
    )
    ident = extract_identity(payload, settings)
    assert ident == Identity(tenant_id="tenant-b", subject="user-9", roles=("viewer",))