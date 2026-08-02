"""Gate 1: signature, exp, iss, aud, alg:none, and claim-presence all enforced."""
from __future__ import annotations

import pytest

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.auth.verify import AuthError, verify_token
from tests.helpers import tokens


def test_valid_token_yields_identity(verifier, settings):
    token = tokens.mint_token(tenant_id="tenant-a", sub="user-1")
    ident = verify_token(token, verifier, settings)
    assert isinstance(ident, Identity)
    assert ident.tenant_id == "tenant-a"
    assert ident.subject == "user-1"
    assert ident.roles == ("agent",)


def test_expired_token_rejected(verifier, settings):
    token = tokens.mint_expired_token()
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "token_expired"


def test_wrong_audience_rejected(verifier, settings):
    token = tokens.mint_wrong_aud_token()
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "bad_audience"


def test_alg_none_rejected(verifier, settings):
    token = tokens.mint_alg_none_token()
    with pytest.raises(AuthError):
        verify_token(token, verifier, settings)


def test_bad_signature_rejected(verifier, settings):
    # Mint with a different secret; signature won't validate against Settings'.
    token = tokens.mint_token(secret="a-different-secret")
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "invalid_token"


def test_missing_tenant_claim_rejected(verifier, settings):
    # Valid signature, but drop the tenant claim -> mapping failure -> AuthError.
    token = tokens.mint_token(tenant_id="x")
    # Re-mint without tenant_id by overriding the payload directly:
    import jwt

    payload = tokens.TokenClaims().to_payload()
    del payload["tenant_id"]
    token = jwt.encode(payload, tokens.TEST_SECRET, algorithm=tokens.TEST_ALG)
    with pytest.raises(AuthError) as exc:
        verify_token(token, verifier, settings)
    assert exc.value.reason_code == "missing_tenant"


def test_verifier_swap_is_isolated(settings):
    # Proves the protocol seam: a stub verifier feeds a payload, mapping still
    # runs. Confirms callers depend on TokenVerifier, not on JWT specifically.
    class StubVerifier:
        def __init__(self, settings):
            self._settings = settings

        @property
        def settings(self):
            return self._settings

        def verify(self, token: str) -> dict:
            return {"tenant_id": "tenant-z", "sub": "user-9", "roles": ["viewer"]}

    ident = verify_token("ignored", StubVerifier(settings), settings)
    assert ident.tenant_id == "tenant-z"
    assert ident.roles == ("viewer",)