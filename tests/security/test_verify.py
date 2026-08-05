"""Gate 1: signature, exp, alg:none, and confirmed-claim presence.
iss/aud are NOT checked (Tymon token doesn't carry them)."""
from __future__ import annotations

import jwt
import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.auth.verify import AuthError, verify_token
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
    token = tokens.mint_token(secret="a-different-secret-that-is-at-least-32-bytes-long")
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