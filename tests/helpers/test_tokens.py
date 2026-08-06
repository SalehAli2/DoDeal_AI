"""Sanity tests for the confirmed test-token helper.

Proves the faker mints the confirmed Tymon shape (sub int, subdomain, database,
no iss/aud), so gate tests can trust it. Gate behaviour is tested in
tests/security/.
"""

from __future__ import annotations

import jwt
import pytest

from tests.helpers.tokens import (
    TEST_ALG,
    TEST_SECRET,
    TokenClaims,
    mint_alg_none_token,
    mint_expired_token,
    mint_token,
)


def _decode(token: str):
    # No iss/aud in the confirmed token; sub is an int so PyJWT's sub check is
    # disabled here too.
    return jwt.decode(
        token,
        TEST_SECRET,
        algorithms=[TEST_ALG],
        options={"verify_sub": False},
    )


def test_mint_token_roundtrips_default_claims():
    payload = _decode(mint_token())
    assert payload["sub"] == 42
    assert payload["subdomain"] == "nasir3"
    assert payload["database"] == "crm_nasir3"
    assert payload["exp"] > payload["iat"]
    assert "iss" not in payload
    assert "aud" not in payload


def test_kwarg_overrides_change_individual_claims():
    payload = _decode(mint_token(subdomain="beta", sub=99))
    assert payload["subdomain"] == "beta"
    assert payload["sub"] == 99


def test_claims_object_is_respected():
    payload = _decode(mint_token(TokenClaims(subdomain="acme", database="crm_acme")))
    assert payload["subdomain"] == "acme"
    assert payload["database"] == "crm_acme"


def test_expired_token_fails_verification():
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(mint_expired_token())


def test_alg_none_token_is_rejected():
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode(mint_alg_none_token())
