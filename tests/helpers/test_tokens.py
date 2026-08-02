"""Sanity tests for the test-token helper — NOT the gates.

These prove the faker mints what we think it mints, so later gate tests can
trust it. Gate behaviour is tested in tests/security/ from Step 9.
"""
from __future__ import annotations

import jwt
import pytest

from tests.helpers.tokens import (
    TEST_ALG,
    TEST_AUD,
    TEST_ISS,
    TEST_SECRET,
    TokenClaims,
    mint_alg_none_token,
    mint_expired_token,
    mint_token,
    mint_wrong_aud_token,
)


def _decode(token: str):
    return jwt.decode(
        token,
        TEST_SECRET,
        algorithms=[TEST_ALG],
        audience=TEST_AUD,
        issuer=TEST_ISS,
    )


def test_mint_token_roundtrips_default_claims():
    payload = _decode(mint_token())
    assert payload["tenant_id"] == "tenant-a"
    assert payload["sub"] == "user-123"
    assert payload["roles"] == ["agent"]
    assert payload["iss"] == TEST_ISS
    assert payload["aud"] == TEST_AUD
    assert payload["exp"] > payload["iat"]


def test_kwarg_overrides_change_individual_claims():
    payload = _decode(mint_token(tenant_id="tenant-b", sub="user-999"))
    assert payload["tenant_id"] == "tenant-b"
    assert payload["sub"] == "user-999"


def test_claims_object_is_respected():
    payload = _decode(mint_token(TokenClaims(tenant_id="tenant-x", roles=["viewer"])))
    assert payload["tenant_id"] == "tenant-x"
    assert payload["roles"] == ["viewer"]


def test_expired_token_fails_verification():
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(mint_expired_token())


def test_wrong_aud_token_fails_verification():
    with pytest.raises(jwt.InvalidAudienceError):
        _decode(mint_wrong_aud_token())


def test_alg_none_token_is_rejected():
    # Decoding an alg:none token while requiring HS256 must be rejected.
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode(mint_alg_none_token())