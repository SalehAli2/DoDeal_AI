"""Gate 1 — verify the inbound token, then map it to an Identity.

The MECHANISM (how a token is verified) sits behind the TokenVerifier protocol
so it can be swapped when the backend confirms what actually reaches us — a user
JWT, a Sanctum token, or a signed service context (Q1, UNCONFIRMED). Today's
implementation is JwtVerifier (HS256, config-driven). Swapping it changes THIS
file's implementation only; callers depend on the protocol.

Security rules enforced here:
  - algorithm read from config; alg:none rejected (never trust the header's alg)
  - signature and exp verified. iss and aud are NOT present on this token and
    are NOT verified -- see JwtVerifier.verify()'s options.
  - exp/nbf/iat allow DODEAL_JWT_LEEWAY_SECONDS of clock skew, and skew-related
    failures get their own reason codes (see JwtVerifier's docstring).
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
    """Tymon JWT verifier — HS256 today (RS256 later via config, no code change).

    CONFIRMED: token has NO iss/aud, so those are not verified. We verify the
    signature, expiry, and reject alg:none via an explicit algorithms allow-list.
    Required-claim presence (sub, subdomain) is enforced by the claim-mapping
    layer downstream, so verify() only asserts what PyJWT checks natively here.

    exp/nbf/iat are checked with jwt_leeway_seconds of clock-skew tolerance, so
    ordinary drift between the CRM's clock and ours is not read as an attack.

    Reason codes raised here (audit only — the client always sees a generic 401):
      - token_expired         exp is in the past, beyond the leeway
      - token_not_yet_valid   iat or nbf is in the future, beyond the leeway
      - invalid_iat           iat is present but not a number
      - missing_required_claim  a claim in `require` is absent (exp)
      - bad_algorithm         the token's alg is not the configured one
      - invalid_token         catch-all: bad signature, malformed, alg:none
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
                algorithms=[s.jwt_algorithm],
                leeway=s.jwt_leeway_seconds,
                options={
                    # The token does not carry `iss` or `aud`; those checks are
                    # disabled. `exp` is present and enforced.
                    #
                    # `sub` is an integer in this token format. PyJWT requires
                    # `sub` to be a string and will reject the token otherwise,
                    # so its `sub` check is disabled here. Claim validation and
                    # normalisation are handled in core/auth/claims.py.
                    "require": ["exp"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": False,
                    "verify_aud": False,
                    "verify_sub": False,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token_expired") from exc
        except jwt.ImmatureSignatureError as exc:
            # iat or nbf in the future beyond the leeway. Distinct from
            # invalid_token so real clock drift is diagnosable in the audit log.
            raise AuthError("token_not_yet_valid") from exc
        except jwt.InvalidIssuedAtError as exc:
            # iat present but not a number — malformed, not merely skewed.
            raise AuthError("invalid_iat") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthError("missing_required_claim") from exc
        except jwt.InvalidAlgorithmError as exc:
            raise AuthError("bad_algorithm") from exc
        except jwt.InvalidTokenError as exc:
            # Catch-all (bad signature, malformed, alg:none) AFTER specific cases.
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
