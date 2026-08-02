"""Gate 1 — verify the inbound token, then map it to an Identity.

The MECHANISM (how a token is verified) sits behind the TokenVerifier protocol
so it can be swapped when the backend confirms what actually reaches us — a user
JWT, a Sanctum token, or a signed service context (Q1, UNCONFIRMED). Today's
implementation is JwtVerifier (HS256, config-driven). Swapping it changes THIS
file's implementation only; callers depend on the protocol.

Security rules enforced here:
  - algorithm read from config; alg:none rejected (never trust the header's alg)
  - signature, exp, iss, aud all verified
  - required claims present (delegated to the claim-mapping layer)
On ANY failure -> AuthError, which the dependency layer (Step 7) turns into a
GENERIC 401. The specific reason is for the audit line only, never the client.
"""
from __future__ import annotations

from typing import Protocol

import jwt

from dodeal_ai.core.auth.claims import ClaimMappingError, Identity, extract_identity
from dodeal_ai.core.config import Settings, get_settings


class AuthError(Exception):
    """Token verification failed. Carries a generic, safe-to-log reason code.
    The client only ever sees a generic 401 — the reason never leaks outward."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class TokenVerifier(Protocol):
    """The swap seam. An implementation takes a raw token string and returns the
    verified claim payload, or raises AuthError. It does NOT do claim mapping —
    that stays in claims.py so there's still one mapping place."""
    @property
    def settings(self) -> Settings: ...
    def verify(self, token: str) -> dict: ...


class JwtVerifier:
    """JWT HS256 verifier — today's assumed mechanism (UNCONFIRMED).

    All parameters come from Settings; nothing hardcoded. alg:none is rejected
    structurally because we pass an explicit algorithms allow-list to PyJWT and
    'none' is never in it.
    """

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
    @property
    def settings(self) -> Settings:
        return self._settings
    def verify(self, token: str) -> dict:
        s = self._settings
        try:
            return jwt.decode(
                token,
                s.jwt_signing_key,
                algorithms=[s.jwt_algorithm],  # allow-list; 'none' excluded
                issuer=s.jwt_issuer,
                audience=s.jwt_audience,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token_expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("bad_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("bad_issuer") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthError("missing_required_claim") from exc
        except jwt.InvalidAlgorithmError as exc:
            raise AuthError("bad_algorithm") from exc
        except jwt.InvalidTokenError as exc:
            # Catch-all for PyJWT (bad signature, malformed, alg:none, etc.)
            # AFTER the specific cases above. Generic reason on purpose.
            raise AuthError("invalid_token") from exc


def verify_token(
    token: str,
    verifier: TokenVerifier,
    settings: Settings | None = None,
) -> Identity:
    payload = verifier.verify(token)
    settings = settings or verifier.settings
    try:
        return extract_identity(payload, settings)
    except ClaimMappingError as exc:
        raise AuthError(exc.reason_code) from exc