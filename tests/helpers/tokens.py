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
import json
import secrets
import time
from dataclasses import dataclass

import jwt  # PyJWT

# Stable per-process test key. Test-only; never used anywhere near prod.
TEST_SECRET = secrets.token_hex(32)
TEST_ALG = "HS256"


@dataclass
class TokenClaims:
    """The CONFIRMED Tymon claim set. sub is an INTEGER. No iss, no aud."""

    sub: int = 42
    subdomain: str = "nasir3"
    database: str = "crm_nasir3"
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