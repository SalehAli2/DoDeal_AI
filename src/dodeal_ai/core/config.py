"""Central runtime configuration.

Every module that needs a claim name, a JWT parameter, or the signing key reads
it from HERE via get_settings(). Nothing hardcodes these values in a second
place — that single-source rule is the whole point of this module.

Fail-closed: jwt_signing_key has no default. If it is not supplied (via a
DODEAL_JWT_SIGNING_KEY env var or the env file), building Settings raises
ConfigError, and the service must refuse to start / report 503 rather than run
without the ability to verify tokens.

See ASSUMPTIONS.md for the UNCONFIRMED items encoded here (token type, claim
names, iss/aud placeholders).
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Required configuration missing or invalid.

    Callers (startup / readiness) treat this as fail-closed: refuse to start or
    return 503. Never swallow it into a running-but-unverifying state.
    """


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DODEAL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,  # config is authoritative; immutable after load
    )

    # --- Gate 1: JWT verification parameters (read by core/auth/verify.py) ---
    # ASSUMPTION: token is a JWT, HS256. UNCONFIRMED (may be Sanctum or a signed
    # service context). Algorithm is config-driven so a swap is one env change.
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "hikal-test-issuer"   # test placeholder
    jwt_audience: str = "dodeal-ai-test"    # test placeholder

    # Required, NO default -> fail-closed if absent. In dev/test this is the
    # self-generated TEST key; in prod it is injected from a secret manager.
    # NEVER a real backend secret committed here.
    jwt_signing_key: str

    # --- Claim-name mapping: the ONE place (read by core/auth/claims.py) ---
    # ASSUMPTION: tenant_id / sub / roles. UNCONFIRMED (may be org_id / user_id /
    # role). When backend sends real sample JSON, change these in exactly one
    # place — here, or via env — and nothing else moves.
    claim_tenant_id: str = "tenant_id"
    claim_subject: str = "sub"
    claim_roles: str = "roles"
    # --- Input guard: max request body size in bytes (config-driven) ---------
    # Placeholder cap; tune per real payload sizes later. Guards memory/cost
    # abuse before any tool/LLM work happens.
    #max_request_body_bytes: int = 1_000_000
    # --- Watchdog: timeout + retry policy for external calls (§6) -----------
    # Placeholder values; tune per real LLM/tool latency later.
    external_call_timeout_seconds: float = 10.0
    external_call_retry_once: bool = True

def _build_settings(**overrides) -> Settings:
    """Construct Settings, converting a missing/invalid-config failure into a
    fail-closed ConfigError. `overrides` (e.g. _env_file=None) let tests build
    deterministically without a stray local .env satisfying the requirement.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise ConfigError(
            "Missing or invalid required configuration; refusing to start."
        ) from exc


@lru_cache
def get_settings() -> Settings:
    """Cached accessor used everywhere. Raises ConfigError (fail-closed) when
    required config is absent. Tests call get_settings.cache_clear() between
    cases so each sees fresh env."""
    return _build_settings()