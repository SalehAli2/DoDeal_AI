"""Gate 1 for the CRM's SERVICE token (register item D1).

The user token (verify.py) says which person is calling. The service token says
the CRM itself is calling, server to server, about a tenant: it is how the
direct, history, brief, measures and admin routes are reached, and a user token
is refused on all of them. The same seam style as TokenVerifier -- a class that
takes a raw token and returns a verified Identity, or raises AuthError with a
fixed reason code for the audit line and a generic 401 for the client.

What a service token must carry, all verified here: iss, aud, the tenant claim,
iat and exp, signed with DODEAL_SERVICE_JWT_ALGORITHM. Its lifetime (exp minus
iat) may not exceed service_jwt_max_lifetime_seconds, so a leaked token is worth
minutes. The previous key is tried only when the current one fails the
signature, which is what lets the CRM rotate without a synchronised release.

Reason codes (audit only):
  service_token_not_configured  no service key is set
  invalid_token       not a JWT at all
  alg_none            the header says alg none
  user_token          a user token presented where a service token is required
  bad_algorithm       an alg other than the configured one
  invalid_signature   neither key verifies the signature
  invalid_issuer / invalid_audience / missing_required_claim
  token_expired / token_not_yet_valid / invalid_iat
  lifetime_exceeded   exp - iat is over the maximum
  missing_subdomain / invalid_tenant_claim   the tenant claim, as for users
  service_key_unusable  the configured key cannot be parsed for the algorithm
"""

from __future__ import annotations

from typing import Any

import jwt
from pydantic import SecretStr

from dodeal_ai.core.auth.claims import Identity, normalise_tenant_label
from dodeal_ai.core.auth.verify import AuthError
from dodeal_ai.core.config import Settings, get_settings

# The subject every service context carries. Not a person: nothing may key a
# per-user limit on it, which is why the direct routes build their scope with
# RequestContext.scope_for_author instead.
SERVICE_SUBJECT = "service"

_PEM_PREFIX = "-----BEGIN"

# PyJWT's failures, most specific first, as audit reason codes. Anything not
# listed is invalid_token. Matched with isinstance, in this order.
_REASONS: tuple[tuple[type[jwt.PyJWTError], str], ...] = (
    (jwt.InvalidKeyError, "service_key_unusable"),
    (jwt.InvalidSignatureError, "invalid_signature"),
    (jwt.ExpiredSignatureError, "token_expired"),
    (jwt.ImmatureSignatureError, "token_not_yet_valid"),
    (jwt.InvalidIssuedAtError, "invalid_iat"),
    (jwt.MissingRequiredClaimError, "missing_required_claim"),
    (jwt.InvalidIssuerError, "invalid_issuer"),
    (jwt.InvalidAudienceError, "invalid_audience"),
    (jwt.InvalidAlgorithmError, "bad_algorithm"),
)


def _reason(exc: jwt.PyJWTError) -> str:
    return next(
        (code for kind, code in _REASONS if isinstance(exc, kind)), "invalid_token"
    )


def service_key_text(key: SecretStr) -> str:
    """The key as PyJWT takes it. THE ONE place a service key is normalised: a
    PEM pasted onto one line with literal \\n escapes becomes a real PEM. A
    shared HS256 secret is returned unchanged, backslashes and all."""
    text = key.get_secret_value()
    if text.startswith(_PEM_PREFIX):
        return text.replace("\\n", "\n")
    return text


class ServiceTokenVerifier:
    """Verifies the CRM's service token and maps it to an Identity whose subject
    is SERVICE_SUBJECT. Built from Settings; swapped in tests by a dependency
    override, exactly as JwtVerifier is."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def configured(self) -> bool:
        return self._settings.service_jwt_signing_key is not None

    def verify(self, token: str) -> Identity:
        s = self._settings
        current = s.service_jwt_signing_key
        if current is None:
            raise AuthError("service_token_not_configured")
        self._screen(token)
        try:
            payload = self._decode(token, current, s.service_jwt_previous_signing_key)
        except jwt.PyJWTError as exc:
            raise AuthError(_reason(exc)) from None
        # PyJWT has already read both as integers, so int() cannot fail here.
        lifetime = int(payload["exp"]) - int(payload["iat"])
        if lifetime > s.service_jwt_max_lifetime_seconds:
            raise AuthError("lifetime_exceeded")
        return self._identity(payload)

    def _screen(self, token: str) -> None:
        """Refusals decided on the UNVERIFIED token, so they get codes of their
        own. Nothing read here is ever trusted: it can only refuse."""
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError:
            raise AuthError("invalid_token") from None
        if str(header.get("alg", "")).lower() == "none":
            raise AuthError("alg_none")
        if self._settings.claim_subject in claims and "iss" not in claims:
            raise AuthError("user_token")

    def _decode(
        self, token: str, current: SecretStr, previous: SecretStr | None
    ) -> dict[str, Any]:
        """The verified payload. The previous key is tried only when the current
        one fails the SIGNATURE; any other failure is the token's, not the key's."""
        if previous is not None:
            try:
                return self._decode_with(token, current)
            except jwt.InvalidSignatureError:
                return self._decode_with(token, previous)
        return self._decode_with(token, current)

    def _decode_with(self, token: str, key: SecretStr) -> dict[str, Any]:
        s = self._settings
        return jwt.decode(
            token,
            service_key_text(key),
            algorithms=[s.service_jwt_algorithm],
            audience=s.service_jwt_audience,
            issuer=s.service_jwt_issuer,
            leeway=s.jwt_leeway_seconds,
            options={
                "require": ["iss", "aud", s.claim_subdomain, "iat", "exp"],
                # No subject is read off a service token, so PyJWT's
                # string-only `sub` rule has nothing to protect here.
                "verify_sub": False,
            },
        )

    def _identity(self, payload: dict[str, Any]) -> Identity:
        s = self._settings
        raw = payload[s.claim_subdomain]
        if not isinstance(raw, str) or not raw:
            raise AuthError("missing_subdomain")
        tenant = normalise_tenant_label(raw)
        if tenant is None:
            raise AuthError("invalid_tenant_claim")
        database = payload.get(s.claim_database, "")
        return Identity(
            tenant=tenant,
            subject=SERVICE_SUBJECT,
            database=database if isinstance(database, str) else "",
        )
