"""Gate 1: signature, exp, alg:none, and confirmed-claim presence.
iss/aud are NOT checked (Tymon token doesn't carry them)."""

from __future__ import annotations

import time

import jwt
import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.auth.verify import AuthError, JwtVerifier, verify_token
from dodeal_ai.core.config import Settings
from tests.helpers import tokens


def test_valid_token_yields_identity(verifier, settings):
    token = tokens.mint_token(subdomain="nasir3", sub=42)
    ident = verify_token(token, verifier, settings)
    assert isinstance(ident, Identity)
    assert ident.tenant == "nasir3"
    assert ident.subject == "42"
    assert ident.database == "crm_nasir3"


def test_token_without_iss_aud_still_passes(verifier, settings):
    # The confirmed token has no iss/aud; it must NOT be rejected for that.
    token = tokens.mint_token()
    ident = verify_token(token, verifier, settings)
    assert ident.tenant == "nasir3"


def test_expired_token_rejected(verifier, settings):
    with pytest.raises(AuthError) as exc:
        verify_token(tokens.mint_expired_token(), verifier, settings)
    assert exc.value.reason_code == "token_expired"


def test_alg_none_rejected(verifier, settings):
    with pytest.raises(AuthError):
        verify_token(tokens.mint_alg_none_token(), verifier, settings)


def test_bad_signature_rejected(verifier, settings):
    token = tokens.mint_token(
        secret="a-different-secret-that-is-at-least-32-bytes-long"
    )
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "invalid_token"


def test_missing_subdomain_claim_rejected(verifier, settings):
    payload = tokens.TokenClaims().to_payload()
    del payload["subdomain"]
    token = jwt.encode(payload, tokens.TEST_SECRET, algorithm=tokens.TEST_ALG)
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "missing_subdomain"


def test_verifier_swap_is_isolated(settings):
    class StubVerifier:
        def __init__(self, s):
            self._settings = s

        @property
        def settings(self):
            return self._settings

        def verify(self, token: str) -> dict:
            return {"sub": 99, "subdomain": "beta", "database": "crm_beta"}

    ident = verify_token("ignored", StubVerifier(settings), settings)
    assert ident.tenant == "beta"
    assert ident.subject == "99"


# --- RS256 (ASSUMPTIONS.md §1.2: the migration must be config-only) ---------
# Every key here is generated inside the test process. No real key is minted
# against or verified against anywhere in this file.


@pytest.fixture
def rs256_keypair():
    return tokens.rsa_test_keypair()


@pytest.fixture
def rs256_settings(rs256_keypair):
    _private_pem, public_pem = rs256_keypair
    return Settings(
        _env_file=None,
        jwt_algorithm="RS256",
        jwt_signing_key=public_pem,
    )


@pytest.fixture
def rs256_verifier(rs256_settings):
    return JwtVerifier(rs256_settings)


def test_rs256_token_verifies_with_public_key(
    rs256_keypair, rs256_settings, rs256_verifier
):
    # The whole point of the migration: flip two env vars, nothing else.
    private_pem, _public_pem = rs256_keypair
    claims = tokens.TokenClaims(
        subdomain="tenant-a", database="crm_tenant_a"
    ).to_payload()
    token = tokens.mint_rs256(claims, private_pem)
    ident = verify_token(token, rs256_verifier, rs256_settings)
    assert isinstance(ident, Identity)
    assert ident.tenant == "tenant-a"
    assert ident.subject == "42"


def test_rs256_config_rejects_hs256_token(rs256_settings, rs256_verifier):
    # Configured for RS256: an HS256 token is off the allow-list, whatever it
    # was signed with.
    token = tokens.mint_token(subdomain="tenant-a")
    with pytest.raises(AuthError) as exc:
        verify_token(token, rs256_verifier, rs256_settings)
    assert exc.value.reason_code == "bad_algorithm"


def test_algorithm_confusion_rejected(rs256_keypair, rs256_settings, rs256_verifier):
    """The classic attack: take the PUBLIC key (which is public), use it as an
    HS256 shared secret, and hope the verifier trusts the header's alg. The
    explicit algorithms allow-list is what stops it."""
    _private_pem, public_pem = rs256_keypair
    claims = tokens.TokenClaims(
        subdomain="tenant-a", database="crm_tenant_a"
    ).to_payload()
    forged = tokens.mint_hs256_unchecked(claims, public_pem)
    with pytest.raises(AuthError) as exc:
        verify_token(forged, rs256_verifier, rs256_settings)
    assert exc.value.reason_code == "bad_algorithm"


def test_rs256_rejects_token_signed_by_other_key(rs256_settings, rs256_verifier):
    other_private_pem, _other_public_pem = tokens.rsa_test_keypair(1)
    claims = tokens.TokenClaims(
        subdomain="tenant-a", database="crm_tenant_a"
    ).to_payload()
    token = tokens.mint_rs256(claims, other_private_pem)
    with pytest.raises(AuthError) as exc:
        verify_token(token, rs256_verifier, rs256_settings)
    assert exc.value.reason_code == "invalid_token"


# --- Clock skew (DODEAL_JWT_LEEWAY_SECONDS, default 30) ---------------------
# The CRM mints on its own clock. Drift inside the leeway must be accepted;
# drift outside it must audit as skew, not as forgery.


def test_iat_slightly_in_future_accepted(verifier, settings):
    token = tokens.mint_token(subdomain="tenant-a", iat=int(time.time()) + 5)
    assert verify_token(token, verifier, settings).tenant == "tenant-a"


def test_nbf_slightly_in_future_accepted(verifier, settings):
    token = tokens.mint_token(subdomain="tenant-a", nbf=int(time.time()) + 5)
    assert verify_token(token, verifier, settings).tenant == "tenant-a"


def test_exp_just_past_accepted_within_leeway(verifier, settings):
    token = tokens.mint_token(subdomain="tenant-a", exp=int(time.time()) - 5)
    assert verify_token(token, verifier, settings).tenant == "tenant-a"


def test_exp_well_past_still_expired(verifier, settings):
    token = tokens.mint_token(subdomain="tenant-a", exp=int(time.time()) - 120)
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "token_expired"


def test_iat_far_in_future_is_not_yet_valid(verifier, settings):
    token = tokens.mint_token(subdomain="tenant-a", iat=int(time.time()) + 120)
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "token_not_yet_valid"


def test_nbf_far_in_future_is_not_yet_valid(verifier, settings):
    token = tokens.mint_token(subdomain="tenant-a", nbf=int(time.time()) + 120)
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "token_not_yet_valid"


def test_zero_leeway_rejects_small_skew():
    """Proves the setting is actually wired into jwt.decode: the same 5s skew
    that test_iat_slightly_in_future_accepted lets through is refused once the
    leeway is 0. (Never configure 0 in production -- see CONTRIBUTING.md.)"""
    strict = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
        jwt_leeway_seconds=0,
    )
    token = tokens.mint_token(subdomain="tenant-a", iat=int(time.time()) + 5)
    with pytest.raises(AuthError) as exc:
        verify_token(token, JwtVerifier(strict), strict)
    assert exc.value.reason_code == "token_not_yet_valid"


def test_malformed_iat_has_its_own_reason_code(verifier, settings):
    token = tokens.mint_token(subdomain="tenant-a", iat="not-a-timestamp")
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "invalid_iat"


def test_auth_error_text_is_the_reason_code_only(verifier, settings):
    """Nothing that reaches a log line may carry token material."""
    now = int(time.time())
    cases = [
        tokens.mint_token(subdomain="tenant-a", exp=now - 120),
        tokens.mint_token(subdomain="tenant-a", iat=now + 120),
        tokens.mint_token(subdomain="tenant-a", nbf=now + 120),
        tokens.mint_token(subdomain="tenant-a", iat="not-a-timestamp"),
        tokens.mint_token(secret="a-different-secret-that-is-at-least-32-bytes"),
        tokens.mint_alg_none_token(),
    ]
    for token in cases:
        with pytest.raises(AuthError) as exc:
            verify_token(token, verifier, settings)
        err = exc.value
        assert str(err) == err.reason_code
        # No segment of the token -- header, payload, or signature -- leaks.
        for segment in token.split("."):
            assert segment == "" or segment not in str(err)
