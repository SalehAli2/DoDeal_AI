"""Test-only helpers for minting fake JWTs.

Simulates what the Hikal backend will eventually send us. NONE of this ships as
production code — it exists so the three gates can be exercised against a
self-generated HS256 key, with no real backend secret wired in.

ASSUMPTION (see ASSUMPTIONS.md): the inbound token is a JWT signed HS256. If the
backend turns out to send a Sanctum token or a signed service context instead,
only the verifier changes (behind the TokenVerifier interface) — these helpers
stay test-only either way.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from functools import cache

import jwt  # PyJWT
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Stable per-process test key. Test-only; never used anywhere near prod.
TEST_SECRET = secrets.token_hex(32)
TEST_ALG = "HS256"


@dataclass
class TokenClaims:
    """The CONFIRMED Tymon claim set. sub is an INTEGER. No iss, no aud."""

    sub: int = 42
    subdomain: str = "tenant-a"
    database: str = "crm_tenant_a"
    ttl_seconds: int = 43200  # 12h, the confirmed lifetime; negative => expired

    def to_payload(self) -> dict:
        now = int(time.time())
        return {
            "sub": self.sub,
            "subdomain": self.subdomain,
            "database": self.database,
            "iat": now,
            "exp": now + self.ttl_seconds,
        }


def mint_token(
    claims: TokenClaims | None = None,
    *,
    secret: str = TEST_SECRET,
    alg: str = TEST_ALG,
    **overrides,
) -> str:
    """Mint a signed JWT for tests.

    Common case: mint_token(tenant_id="tenant-b").
    Full control: mint_token(TokenClaims(roles=["viewer"], ttl_seconds=10)).
    """
    claims = claims or TokenClaims()
    payload = claims.to_payload()
    payload.update(overrides)
    return jwt.encode(payload, secret, algorithm=alg)


def mint_expired_token(**overrides) -> str:
    return mint_token(TokenClaims(ttl_seconds=-60), **overrides)


# --- the CRM's service token (register item D1) ----------------------------
# A second, unrelated HS256 secret: Settings refuses a service key equal to the
# user key, so the two can never be the same value in a test either.
SERVICE_TEST_SECRET = secrets.token_hex(32)
SERVICE_ISSUER = "dodeal-crm"
SERVICE_AUDIENCE = "dodeal-ai"


def service_claims(
    *, subdomain: str = "tenant-a", ttl_seconds: int = 300, **overrides: object
) -> dict:
    """The claim set a service token must carry: iss, aud, subdomain, iat, exp."""
    now = int(time.time())
    claims: dict = {
        "iss": SERVICE_ISSUER,
        "aud": SERVICE_AUDIENCE,
        "subdomain": subdomain,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    claims.update(overrides)
    return claims


def mint_service_token(
    *,
    secret: str = SERVICE_TEST_SECRET,
    alg: str = TEST_ALG,
    subdomain: str = "tenant-a",
    ttl_seconds: int = 300,
    **overrides: object,
) -> str:
    """A signed service token; keyword claims override service_claims()."""
    claims = service_claims(subdomain=subdomain, ttl_seconds=ttl_seconds, **overrides)
    return jwt.encode(claims, secret, algorithm=alg)


def service_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the process Settings for HS256 service tokens from this module."""
    monkeypatch.setenv("DODEAL_SERVICE_JWT_ALGORITHM", TEST_ALG)
    monkeypatch.setenv("DODEAL_SERVICE_JWT_SIGNING_KEY", SERVICE_TEST_SECRET)


"""
def mint_wrong_aud_token(**overrides) -> str:
    return mint_token(TokenClaims(aud="not-dodeal-ai"), **overrides)
"""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_alg_none_token(claims: TokenClaims | None = None, **overrides) -> str:
    """Hand-crafted alg:none token — the classic 'drop the signature' attack.

    Built by hand (not via PyJWT) so the test is version-proof: some PyJWT
    releases refuse to *encode* alg:none, but we still need to prove Gate 1
    *rejects* it on decode.
    """
    claims = claims or TokenClaims()
    payload = claims.to_payload()
    payload.update(overrides)
    header = {"alg": "none", "typ": "JWT"}
    return ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
            "",  # empty signature
        ]
    )


# --- RS256 -----------------------------------------------------------------
# The agreed migration (ASSUMPTIONS.md §1.2) is HS256 -> RS256 by config alone.
# These helpers prove that end to end without any real key: the pair is
# generated inside the test process and never leaves it.


@cache
def rsa_test_keypair(_slot: int = 0) -> tuple[str, str]:
    """(private_pem, public_pem) for an ephemeral 2048-bit RSA test key.

    Cached per `_slot` purely to keep the suite fast — key generation is the
    slow part. Pass a different `_slot` when a test needs a SECOND, unrelated
    keypair (the wrong-key case). Test-only; never a real key.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def mint_rs256(claims: dict, private_pem: str) -> str:
    """Sign an already-built claim dict with an RSA private key (RS256)."""
    return jwt.encode(claims, private_pem, algorithm="RS256")


def mint_hs256_unchecked(claims: dict, secret: str) -> str:
    """HS256-sign `claims` with ANY secret, bypassing PyJWT's encode-side guards.

    Needed for the algorithm-confusion case: the attacker HMACs the token with
    the RSA *public* key (which is public) and hopes the verifier trusts the
    header's alg. PyJWT refuses to *encode* that — it rejects a PEM as an HMAC
    secret — but an attacker is not using PyJWT. Built by hand for the same
    reason mint_alg_none_token is: we still need to prove Gate 1 *rejects* it.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(claims, separators=(",", ":")).encode()),
        ]
    ).encode()
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{signing_input.decode()}.{_b64url(signature)}"
