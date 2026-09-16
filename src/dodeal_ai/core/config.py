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

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError

# Connections each Redis pool holds beyond one per admitted request: the /ready
# pings, which the in-flight cap does not count, plus slack. Not a setting --
# the pool size itself is, through redis_max_connections below.
REDIS_POOL_HEADROOM = 4


class ConfigError(RuntimeError):
    """Required configuration missing or invalid.

    Callers (startup / readiness) treat this as fail-closed: refuse to start or
    return 503. Never swallow it into a running-but-unverifying state.
    """


class LLMProvider(str, Enum):
    """Providers get_llm_client() can build. A typo in DODEAL_LLM_PROVIDER
    fails at settings load (ConfigError), not at the first model call.

    Membership here means "the name parses", NOT "an adapter exists":
    build_llm_client() refuses ANTHROPIC and GEMINI by name until their
    adapters land (76.3), which is a clearer failure than a rejected enum.
    """

    ANTHROPIC = "anthropic"
    GROQ = "groq"
    OPENAI = "openai"
    GEMINI = "gemini"


class ModelProfile(BaseModel):
    """One named task's model choice. Validated HERE, so a bad profile fails at
    settings construction rather than at the first model call (report R17)."""

    model_config = {"frozen": True}

    provider: LLMProvider
    # No default: a profile that names no model is a config error, not a
    # silent fall-through to llm_model.
    model: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_output_tokens: int | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DODEAL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,  # config is authoritative; immutable after load
    )

    # --- Gate 1: JWT verification parameters (read by core/auth/verify.py) ---
    # CONFIRMED: Tymon JWT, HS256 (RS256 requested & agreed,
    # awaiting provisioning). Token carries NO iss and NO aud — those checks are
    # removed. exp IS present and still verified.
    jwt_algorithm: str = "HS256"

    # Clock-skew tolerance for exp/nbf/iat, in seconds. The CRM mints tokens on its own clock;
    # without leeway a few seconds of drift rejects valid tokens with a reason code that looks like
    # forgery. 30s is the conventional value. Do not set to 0.
    jwt_leeway_seconds: int = 30

    # Required, NO default -> fail-closed if absent. In dev/test this is the
    # self-generated TEST key; in prod it is injected from a secret manager.
    # NEVER a real backend secret committed here.
    # HS256: the shared secret. RS256: the CRM's PUBLIC key (PEM). Field name kept for env
    # stability; it is the verification key.
    # SecretStr, so repr(settings) prints `**********` (register item 91). Read
    # in exactly ONE place: JwtVerifier.verify, with .get_secret_value().
    jwt_signing_key: SecretStr

    # --- Claim-name mapping: the ONE place (read by core/auth/claims.py) ---
    # Tymon JWT carries sub (user id) + subdomain (tenant) +
    # database. NO roles claim exists in the token. Names are still config-driven
    claim_subject: str = "sub"
    claim_subdomain: str = "subdomain"
    claim_database: str = "database"

    # --- Input guard: max request body size in bytes (middleware/body_limit.py)
    # The largest body the app will read. Above it the request is refused with
    # 413 payload_too_large at the outermost middleware, before Gate 1.
    #
    # 64 kB because the largest real note plus its lead fields is well under
    # 16 kB: this is four times that, so it is a MEMORY BOUND and not a content
    # rule -- a body under the cap is not thereby valid, and the schemas still
    # decide that. It does not replace the edge limit (register item 43); it is
    # the bound that holds when the edge has none.
    #
    # ge=1: too small a value makes every judgement a 413, an outage that looks
    # like the CRM sending bad requests, so the row in .env.example carries the
    # default rather than leaving it to be guessed.
    max_request_body_bytes: int = Field(default=65_536, ge=1)

    # Backend service credentials, ONE PER TENANT (integration guide §1: a key is valid only against
    # its own tenant host). Keyed by tenant subdomain. Parsed from JSON in the env var, e.g.
    #   DODEAL_DD_API_KEYS={"tenant-a":"<key>","tenant-b":"<key>"}
    # No default value exists for any tenant: an unknown tenant fails closed in tools/keys.py.
    # Empty map = nothing can reach the backend; startup logs this at ERROR (main.py).
    dd_api_keys: dict[str, SecretStr] = {}
    backend_base_domain: str = "dodealcrm.com"
    # The scheme LeadsClient builds every backend URL with. https by default because the per-tenant
    # DD-API-KEY rides each request and must never cross the wire in clear. http is DEMO ONLY -- a
    # laptop has no certificate for tenant-a.dodealcrm.com; set in production it leaks every key.
    backend_scheme: Literal["https", "http"] = "https"
    # The base domain requests to THIS service arrive under: Gate 2 requires
    # Host == "<tenant>.<inbound_base_domain>". Same as the backend's domain today; kept separate
    # because the host of arrival is an open question with the backend (design note, Decision 1,
    # question 2) and may become e.g. "ai.dodealcrm.com" without touching the backend URL.
    inbound_base_domain: str = "dodealcrm.com"
    # Where versioned prompt files are read from. None = the copies shipped inside the package
    # (src/dodeal_ai/prompts). Set only for local prompt iteration; production uses the package.
    prompts_dir: Path | None = None
    # --- Watchdog: timeout + retry policy for external calls (§6) -----------
    # Placeholder values; tune per real LLM/tool latency later.
    external_call_timeout_seconds: float = 10.0
    external_call_retry_once: bool = True

    # --- LLM seam (core/llm/; Unit A owns it, Unit B consumes it) ------------
    # None = not configured; the factory refuses to build a client.
    llm_provider: LLMProvider | None = None
    # Exact pinned model id, set per deployment. NO drifting default: the
    # factory refuses an empty value. Stored on every output as model_version.
    llm_model: str = ""
    # Per-call timeout for a model call, passed as timeout= into
    # call_with_watchdog with retry=False. DELIBERATELY separate from
    # external_call_timeout_seconds (10s, sized for a CRM fetch). Never raise
    # the global to fit a model call.
    llm_timeout_seconds: float = 60.0
    # Default output ceiling. Headroom for Arabic, which costs roughly 1.5-3x
    # the tokens of equivalent English. Tasks override per call.
    llm_max_output_tokens: int = 1024
    # Per-task model choices, keyed by profile name (core/llm/profiles.py), read
    # as JSON, e.g. DODEAL_LLM_PROFILES={"unit_a.classify":{"provider":
    # "anthropic","model":"<id>","temperature":0}}. A name that is not here
    # falls back to the llm_provider/llm_model pair above at temperature 0,
    # which is what a single-model deployment configures and nothing else.
    llm_profiles: dict[str, ModelProfile] = {}
    # The provider API key. SecretStr so a stray repr or log of Settings prints
    # `**********`; None (the default) means no key, and build_llm_client
    # refuses rather than sending an unauthenticated call that 401s at cost.
    # Read in exactly ONE place -- the adapter's header builder.
    llm_api_key: SecretStr | None = None
    # Proxy/gateway override for the provider base URL. None (the default) uses
    # the provider constant in core/llm/openai_compatible.py; set it only to put
    # a proxy in front. A wrong value sends every prompt to the wrong host, so
    # it is never logged and never carried on an exception.
    llm_base_url: str | None = None

    # Redis connections. Two named connections so code never guesses which
    # instance it is using: a queue connection and a cost/quota connection.
    # Local Redis by default; real hosts come from DevOps later. Different
    # logical DBs (0 and 1) keep the two namespaces separate.
    redis_queue_url: str = "redis://localhost:6379/0"
    redis_cost_url: str = "redis://localhost:6379/1"
    # Per-request operational state for the feature units: idempotency
    # reservations, clarification rate limits, per-note attempt counters. A
    # THIRD logical DB, not a third namespace inside the cost DB: these keys
    # have different lifetimes and a different failure policy from the cost
    # counters (idempotency fails CLOSED, the cost cap fails open), and sharing
    # a DB would make a flush aimed at one of them hit the other.
    redis_operational_url: str = "redis://localhost:6379/2"
    # --- Redis connection budget (core/redis.py; audit H3) ------------------
    # PROVISIONAL, all four; gt=0 on every one, so a non-positive value fails
    # closed at construction rather than at the first command.
    #
    # Far shorter than the read timeout: reaching a listening socket on the
    # same network is sub-millisecond, so a slow connect means the host is
    # gone, not busy.
    redis_connect_timeout_seconds: float = Field(default=0.25, gt=0)
    # What one Redis command may take. This is what a Redis outage costs a
    # request before the cost gate fails open.
    redis_socket_timeout_seconds: float = Field(default=1.0, gt=0)
    # Optional override of each BOUNDED pool's size; read it as redis_pool_size.
    # Unset = max_inflight + REDIS_POOL_HEADROOM, one per admitted request.
    # Below max_inflight, load waits on the pool and is refused: 503s at db2.
    redis_max_connections: int | None = Field(default=None, gt=0)
    # How long a caller waits for a free connection before the pool refuses.
    # Without it, "bounded" would mean "blocks forever at the cap".
    redis_pool_acquire_timeout_seconds: float = Field(default=1.0, gt=0)

    # --- Redis circuit breaker (core/breaker.py) ----------------------------
    # Consecutive store failures that open a connection's breaker. PROVISIONAL:
    # 5 rides out a blip, then stops paying a dead store's timeout per request.
    # Too low opens on a blip (db2: 503s for a window); too high pays each timeout.
    breaker_failure_threshold: int = Field(default=5, gt=0)
    # How long an open breaker refuses before it lets ONE probe through.
    # PROVISIONAL: 30s spares a dead store without leaving a live one refused long.
    # Too long is that many seconds of 503s on db2; too short re-probes a dead one.
    breaker_open_seconds: float = Field(default=30.0, gt=0)

    # Cost/quota caps (placeholder values; tune to real budgets later).
    # Counters reset each window. A request over either cap is denied (429).
    cost_per_tenant_limit: int = 10000
    cost_per_user_limit: int = 1000
    cost_window_seconds: int = 86400  # 24h

    # Token budget (core/cost/limiter.py), on the SAME window as the request
    # caps above but on its own keys: "requests made" and "tokens spent" are
    # different quantities and one must never stand in for the other.
    # PROVISIONAL -- no real provider has run, so these are placeholders.
    cost_tokens_per_tenant_limit: int = Field(default=5_000_000, gt=0)
    cost_tokens_per_user_limit: int = Field(default=500_000, gt=0)
    # The fraction of a limit at which a running total earns one WARNING.
    # STRICTLY between 0 and 1: 0 warns on the first token, 1 warns only once
    # the budget is already spent, and neither is a warning.
    cost_token_warning_ratio: float = Field(default=0.9, gt=0.0, lt=1.0)

    # --- Load shedding (middleware/inflight.py) -----------------------------
    # The number of requests allowed INSIDE the app at once. The next one is
    # refused immediately with 503 load_shed rather than queued behind work the
    # event loop cannot get to -- a queue that grows without a bound turns one
    # slow dependency into every request timing out at the caller.
    #
    # PROVISIONAL. 32 is a placeholder chosen to be obviously a placeholder, not
    # a measurement: the real number is a function of the model call's latency
    # and the memory one in-flight judgement holds, and neither has been
    # measured against a real provider. The LOAD LANE sets it. Until then, treat
    # a load_shed line in production as "this number is wrong", not as capacity.
    #
    # gt=0: zero would refuse every request including the first, which is a
    # config typo that looks exactly like an outage. It fails closed at startup
    # (ConfigError) instead.
    max_inflight: int = Field(default=32, gt=0)

    # --- Judgement deadline (units/structured_intelligence/pipeline.py) -----
    # One end-to-end budget per judgement, fetch included; past it, 503.
    # PROVISIONAL until Q16: at or below the CRM's own timeout, set with max_inflight.
    # Above the CRM's timeout, the CRM abandons requests we go on to finish.
    judgement_deadline_seconds: float = Field(default=25.0, gt=0)

    # --- Logging (core/logging_config.py) ------------------------------
    # Effective level for the "dodeal_ai" logger tree (audit, error, cost,
    # resilience, validation, ...). Third-party libraries are unaffected --
    # they stay at the root logger's WARNING default.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @property
    def redis_pool_size(self) -> int:
        """Connections per Redis pool: the explicit override when one is set,
        otherwise one per request the in-flight cap admits, plus headroom.

        Derived rather than defaulted, so raising DODEAL_MAX_INFLIGHT raises the
        pool with it. A fixed pool below the cap is register item 81: requests
        the app has already admitted queue on the pool and are refused, which is
        load turning into 503s on a Redis that is perfectly healthy.
        """
        if self.redis_max_connections is not None:
            return self.redis_max_connections
        return self.max_inflight + REDIS_POOL_HEADROOM


def _build_settings(**overrides) -> Settings:
    """Construct Settings, converting a missing/invalid-config failure into a
    fail-closed ConfigError. `overrides` (e.g. _env_file=None) let tests build
    deterministically without a stray local .env satisfying the requirement.
    """
    try:
        return Settings(**overrides)
    except (ValidationError, SettingsError):
        # SettingsError, not ValidationError, is what a JSON field with
        # unparseable text raises (dd_api_keys, llm_profiles). Both mean the
        # same thing here: the configuration is unusable. `from None`: a
        # validation error quotes the rejected input, which can be a secret.
        raise ConfigError(
            "Missing or invalid required configuration; refusing to start."
        ) from None


@lru_cache
def get_settings() -> Settings:
    """Cached accessor used everywhere. Raises ConfigError (fail-closed) when
    required config is absent. Tests call get_settings.cache_clear() between
    cases so each sees fresh env."""
    return _build_settings()
