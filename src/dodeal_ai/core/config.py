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
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError

# Connections each Redis pool holds beyond one per admitted request: the /ready
# pings, which the in-flight cap does not count, plus slack. Not a setting --
# the pool size itself is, through redis_max_connections below.
REDIS_POOL_HEADROOM = 4

# How far inside a call job's timeout one STT request must end: the run stops
# itself 60 s before arq's cancel, and the failure needs the rest to be
# recorded as a retry. A request timeout closer than this is refused.
STT_TIMEOUT_MARGIN_SECONDS = 120


class ConfigError(RuntimeError):
    """Required configuration missing or invalid.

    Callers (startup / readiness) treat this as fail-closed: refuse to start or
    return 503. Never swallow it into a running-but-unverifying state.
    """


class LLMProvider(str, Enum):
    """Providers get_llm_client() can build. A typo in DODEAL_LLM_PROVIDER
    fails at settings load (ConfigError), not at the first model call.

    Membership here means "the name parses", NOT "an adapter exists":
    build_llm_client() refuses ANTHROPIC and GEMINI by name while they have
    no adapter, which is a clearer failure than a rejected enum.
    """

    ANTHROPIC = "anthropic"
    GROQ = "groq"
    OPENAI = "openai"
    GEMINI = "gemini"


# A registry provider's name (core/llm/routing.py): lower case, short, and never
# one of LLMProvider's values or "default", so a profile's provider is never
# ambiguous between the registry and the DODEAL_LLM_* pair.
PROVIDER_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
RESERVED_PROVIDER_NAMES = frozenset({*(p.value for p in LLMProvider), "default"})


class ProviderSpec(BaseModel):
    """One registry provider: how to reach it and where its key is. The key
    itself is never here -- only the NAME of the variable that holds it."""

    model_config = {"frozen": True, "extra": "forbid"}

    kind: Literal["openai_compatible"]
    base_url: str = Field(min_length=1, pattern=r"^https?://")
    # An environment variable name, read in one place (routing._provider_key).
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    # The provider's own call timeout; at most llm_timeout_seconds (routing).
    timeout_seconds: float = Field(gt=0)


class ModelProfile(BaseModel):
    """One named task's model choice. Validated HERE, so a bad profile fails at
    settings construction rather than at the first model call (report R17)."""

    model_config = {"frozen": True}

    # An LLMProvider value names the DODEAL_LLM_* pair's provider, as before;
    # any other name is a registry provider (DODEAL_LLM_PROVIDERS).
    provider: Annotated[
        LLMProvider | Annotated[str, Field(pattern=PROVIDER_NAME_PATTERN)],
        Field(union_mode="left_to_right"),
    ]
    # No default: a profile that names no model is a config error, not a
    # silent fall-through to llm_model.
    model: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_output_tokens: int | None = None
    # How hard a reasoning model thinks; None for a model that does not reason.
    # Set, temperature is not sent (reasoning models refuse it) and a Unit B
    # pass takes its reasoning ceiling. On a plain model the provider 400s.
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    # json_object asks for any JSON object; json_schema also sends the calling
    # pass's own schema where it has one. Either way the answer is validated in
    # code, so a provider that ignores it costs a reprompt, never trust.
    response_format: Literal["json_object", "json_schema"] = "json_object"
    # A fixed sampling seed for a provider that honours one; None sends none.
    # Reproducibility only: the same seed can still give a different answer,
    # and nothing downstream may assume it does not.
    seed: int | None = None
    # The profile tried ONCE when this one's call got no response body (a
    # connect error, 429, 503, an open breaker); None tries nothing. Followed
    # one hop only, so a chain can never become a retry loop.
    fallback_profile: str | None = Field(default=None, min_length=1)


# The speech-to-text engines an STT profile may name (units/call_intelligence/
# transcriber.py); "fake" is the demo's, allowed only as CALL_STT_PROVIDER.
type SttProvider = Literal["gemini", "openai_compatible", "diarized_http"]


class SttProfile(BaseModel):
    """One named speech-to-text choice a tenant may pick (unit_b stt_profile).
    Its key is read from the variable it names, never from the JSON."""

    model_config = {"frozen": True, "extra": "forbid"}

    provider: SttProvider
    # None uses the engine's own endpoint; required by the HTTP engines.
    base_url: str | None = Field(default=None, pattern=r"^https?://")
    model: str = Field(min_length=1)
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")


class ModelPrice(BaseModel):
    """One model's price, USD per million tokens (register item "cost")."""

    model_config = {"frozen": True}

    input: float = Field(ge=0)
    cached_input: float = Field(ge=0)
    output: float = Field(ge=0)


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

    # --- The CRM's service token (core/auth/service.py, register item D1) ---
    # The algorithm the CRM signs its service token with. RS256 by default so
    # we hold only a public key; HS256 means a shared secret that can mint.
    # A value the CRM does not sign with refuses every service call (401).
    service_jwt_algorithm: Literal["RS256", "ES256", "HS256"] = "RS256"
    # Verifies the service token: a public PEM for RS/ES (one line with \n
    # escapes is accepted), the shared secret for HS. None = no service routes:
    # /ready 503s and every direct, brief and admin call is 401.
    service_jwt_signing_key: SecretStr | None = None
    # The key being rotated out, tried only when the current one fails the
    # signature. None outside a rotation; left set after one, a retired key
    # still verifies tokens for as long as it stays here.
    service_jwt_previous_signing_key: SecretStr | None = None
    # The `iss` the CRM puts on a service token. Fixed by agreement with the
    # backend; a mismatch refuses every service call with invalid_issuer.
    service_jwt_issuer: str = Field(default="dodeal-crm", min_length=1)
    # The `aud` a service token must name: this service. It stops a token minted
    # for another CRM consumer being replayed here; a mismatch refuses them all.
    service_jwt_audience: str = Field(default="dodeal-ai", min_length=1)
    # The longest exp minus iat accepted, in seconds. 300 keeps a stolen token
    # worth five minutes; too low refuses the CRM's own tokens as lifetime_exceeded.
    service_jwt_max_lifetime_seconds: int = Field(default=300, gt=0)

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
    # Where per-tenant config files live: one `<tenant>.json` each, a section per
    # unit (register item 97). None = every unit's default for every tenant. A
    # wrong directory or an invalid file refuses startup, naming the tenant.
    tenant_config_dir: Path | None = None
    # How long each process trusts the runtime override it last read from db2
    # before reading it again (register item 97). 30 s is how late a changed rule
    # reaches the other pods; too high and an administrator's change looks lost.
    tenant_config_cache_seconds: float = Field(default=30.0, gt=0)
    # Where the brief and the measures read judgements and people (register
    # item 143): "crm" is the two CRM reads in tools/crm_reads.py, "none" is no
    # source (503). Off by default: the endpoints are unconfirmed (Q23).
    brief_source: Literal["none", "crm"] = "none"
    # --- Unit B: call recordings (core/audio_download.py) -------------------
    # The whole of one recording's download, connect to last byte. 60 s fetches
    # 200 MiB on a modest link; too low fails long calls as audio_download_timeout
    # (retried, and paid for again in time), too high holds a worker on a stall.
    call_download_timeout_seconds: float = Field(default=60.0, gt=0)
    # Processing attempts a call job gets before it is dead-lettered (register
    # item 35). 2 rides out one bad download or one failed transcription, never
    # more than one re-paid. Higher re-fetches a dead link; 1 never retries.
    call_max_tries: int = Field(default=2, ge=1)
    # arq's timeout for one run of a call-queue task; the run stops itself 60 s
    # sooner (worker.py), so under 120 is refused. 1800 s holds a long call's
    # transcription; too low kills paid work mid-flight, too high holds a slot.
    call_job_timeout_seconds: int = Field(default=1800, ge=120)
    # How long a call job's record lives after its last transition (core/jobs.py):
    # a week outlives any retry and pause, so nothing is stranded forever. Too
    # low loses a paused job's record; too high keeps a dead job's link longer.
    call_job_record_ttl_seconds: int = Field(default=604_800, gt=0)
    # Each tenant's callback signing secret, JSON {"tenant": "secret"}; SecretStr
    # so a repr prints stars. Read once, in core/callbacks.py. A tenant missing
    # here gets NO callback -- never an unsigned one -- and polls GET instead.
    call_callback_secrets: dict[str, SecretStr] = {}
    # DEMO ONLY: admit http links and loopback addresses for call audio and
    # callbacks, so a laptop can serve both. False refuses them; True anywhere
    # real lets a push make this service fetch from and post to itself.
    call_demo_allow_local_audio: bool = False
    # The speech-to-text engine the "default" STT profile runs on. "fake" is
    # the demo's and starts only under CALL_DEMO_ALLOW_LOCAL_AUDIO; a worker
    # with it and the flag off refuses to start rather than invent words.
    call_stt_provider: Literal[
        "fake", "gemini", "openai_compatible", "diarized_http"
    ] = "fake"
    # STT profiles a tenant may name besides "default", JSON {"<name>":
    # {"provider":..,"base_url":..,"model":..,"api_key_env":"<VAR>"}}. {} =
    # default only; a tenant naming one not here is refused (422).
    call_stt_profiles: dict[
        Annotated[str, Field(pattern=PROVIDER_NAME_PATTERN)], SttProfile
    ] = {}
    # The "default" STT profile's endpoint. None uses the engine's own (Gemini's
    # public API); the HTTP engines need one. A wrong one fails every call's
    # transcription, retryable once, never billed twice.
    call_stt_base_url: str | None = Field(default=None, pattern=r"^https?://")
    # The "default" STT profile's model, pinned: Google's transcription model.
    # Carried on every transcript and priced by STT_PRICES under this name; a
    # wrong one is refused by the engine (a permanent failure, unpaid).
    call_stt_model: str = Field(default="gemini-3.5-transcribe", min_length=1)
    # The "default" STT profile's key. SecretStr, read in one place
    # (units/call_intelligence/stt.py). Empty with a paid engine refuses the
    # worker's start rather than fail every call.
    call_stt_api_key: SecretStr | None = None
    # One speech-to-text request's timeout, every profile and engine alike. 600 s
    # holds an hour's call; too low fails long calls (retried once, paid again),
    # and within 120 s of CALL_JOB_TIMEOUT_SECONDS refuses to start.
    call_stt_timeout_seconds: int = Field(default=600, gt=0)

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
    # How long a model call waits for a free connection in the pooled client
    # (register item 112). 1.0 s fails a burst fast as provider_pool_exhausted;
    # too low refuses a brief queue, too high hides a full pool inside the call.
    llm_pool_acquire_timeout_seconds: float = Field(default=1.0, gt=0)

    # --- Fallback provider (register item 21), all optional -----------------
    # The provider tried once when the primary gave no response body (connect
    # error, 429, 503, breaker open). None = no fallback, the safe default; a
    # half-configured fallback refuses to start rather than silently not exist.
    llm_fallback_provider: LLMProvider | None = None
    # The fallback's exact pinned model id, used for every task: the profile
    # table is the primary's. Empty with a provider set refuses to start, as the
    # primary's does; a wrong id costs a failed call only when the primary fails.
    llm_fallback_model: str = ""
    # The fallback provider's key. SecretStr, never logged; read only by the
    # fallback client's header builder. Missing with a provider set refuses to
    # start rather than send unauthenticated calls at the worst moment.
    llm_fallback_api_key: SecretStr | None = None
    # Proxy override for the fallback's base URL. None uses that provider's
    # constant. A wrong value sends fallback prompts to the wrong host, so it is
    # never logged and never carried on an exception, as for the primary.
    llm_fallback_base_url: str | None = None

    # --- Provider registry and model routes (core/llm/routing.py) -----------
    # Every provider besides the DODEAL_LLM_* pair, JSON {"<name>": {"kind":
    # "openai_compatible","base_url":..,"api_key_env":"<VAR>","timeout_seconds":
    # ..}}. {} = the pair only; a key variable left empty refuses startup.
    llm_providers: dict[
        Annotated[str, Field(pattern=PROVIDER_NAME_PATTERN)], ProviderSpec
    ] = {}
    # Routes a tenant may choose, JSON {"<route>": {"<pass>": "<profile>"}}. The
    # route "default" is built from DODEAL_LLM_* and cannot be set here; a pass
    # a route leaves out is served as on "default". A wrong name refuses startup.
    model_routes: dict[
        Annotated[str, Field(pattern=PROVIDER_NAME_PATTERN)], dict[str, str]
    ] = {}

    # Redis connections. Two named connections so code never guesses which
    # instance it is using: a queue connection and a cost/quota connection.
    # Local Redis by default; real hosts come from DevOps later. Different
    # logical DBs (0 and 1) keep the two namespaces separate.
    #
    # All four URLs are SecretStr (register item 158): a real one carries the
    # store's password, so repr(settings) prints stars. Each is read in exactly
    # ONE place, core/redis.py::redis_url, with .get_secret_value().
    redis_queue_url: SecretStr = SecretStr("redis://localhost:6379/0")
    redis_cost_url: SecretStr = SecretStr("redis://localhost:6379/1")
    # Per-request operational state for the feature units: idempotency
    # reservations, clarification rate limits, per-note attempt counters. A
    # THIRD logical DB, not a third namespace inside the cost DB: these keys
    # have different lifetimes and a different failure policy from the cost
    # counters (idempotency fails CLOSED, the cost cap fails open), and sharing
    # a DB would make a flush aimed at one of them hit the other.
    redis_operational_url: SecretStr = SecretStr("redis://localhost:6379/2")
    # Unit B's job state and results (core/jobs.py, register item 50): a FOURTH
    # logical DB, because jobs fail CLOSED and outlive a request by days. A URL
    # shared with db2 would let a flush of reservations erase every call job.
    redis_jobs_url: SecretStr = SecretStr("redis://localhost:6379/3")
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
    # The tenant's request cap for HISTORY judgements, per window, on its own
    # counter (register item 127): a backfill of old notes never spends the live
    # cap. 50000 is a backfill's worth; too low stalls a backfill with 429s.
    cost_history_per_tenant_limit: int = Field(default=50_000, gt=0)
    # The tenant's request cap for the brief, measures and admin routes, per
    # window, on its own counter (register item 153): reads never spend the live
    # cap. 5000 covers a CRM's dashboards; too low 429s them, too high bounds none.
    cost_reads_per_tenant_limit: int = Field(default=5_000, gt=0)
    # The tenant's call-job pushes per window, on `cost:calls:tenant` (register
    # item 50): a job is minutes of audio and paid passes, so its own cap, never
    # the live one. 2000 is a busy sales floor's day; too low 429s real calls.
    cost_calls_per_tenant_limit: int = Field(default=2_000, gt=0)
    # The tenant's call re-analyses per window, on `cost:reanalysis:tenant`: a
    # list or prompt change re-runs many stored calls at once, so its own cap,
    # never the pushes'. 20000 is a month of calls; too low 429s a re-run.
    cost_reanalysis_per_tenant_limit: int = Field(default=20_000, gt=0)

    # Token budget (core/cost/limiter.py), on the SAME window as the request
    # caps above but on its own keys: "requests made" and "tokens spent" are
    # different quantities and one must never stand in for the other.
    # PROVISIONAL -- no real provider has run, so these are placeholders.
    cost_tokens_per_tenant_limit: int = Field(default=5_000_000, gt=0)
    cost_tokens_per_user_limit: int = Field(default=500_000, gt=0)
    # The tenant's separate token budget for HISTORY judgements (register item
    # 127): old notes scored in bulk, charged here and never to the live pair,
    # so a backfill cannot starve today's judgements. Too low stalls a backfill.
    cost_tokens_history_per_tenant_limit: int = Field(default=20_000_000, gt=0)
    # The tenant's token budget for CALL analysis (register item 105), per
    # window, charged to `tokens:calls:tenant` and never the live pair. A call
    # transcript is long; too low pauses every call job for the rest of the day.
    cost_tokens_calls_per_tenant_limit: int = Field(default=20_000_000, gt=0)
    # Seconds of call audio a tenant may send to speech-to-text per window,
    # charged on download to `audio_seconds:calls:tenant`. 360000 is 100 hours;
    # too low pauses a busy floor's calls, too high bounds no bill.
    cost_audio_seconds_per_tenant_limit: int = Field(default=360_000, gt=0)
    # --- Prices (core/cost/spend.py) ---------------------------------------
    # USD per million tokens by the model name a provider REPORTS, JSON
    # {"<model>":{"input":..,"cached_input":..,"output":..}}. Empty: every
    # cost is null with one warning per task; a wrong price is a wrong bill.
    model_prices: dict[str, ModelPrice] = {}
    # USD per audio minute by speech-to-text model name, JSON {"<model>": 0.006}.
    # Empty: every call's cost is null with one warning; a wrong value
    # misstates every call's cost, never what is charged against a budget.
    stt_prices: dict[str, Annotated[float, Field(ge=0)]] = {}
    # The name of the price table above, on every outcome line beside its cost,
    # so a figure is read against the table that made it. "unset" says there is
    # no table; left stale after a price change, old and new costs mix silently.
    price_table_version: str = Field(default="unset", min_length=1)

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
    # History judgements allowed in flight at once, per process, INSIDE
    # max_inflight (register item 127). 8 leaves live notes three quarters of the
    # slots; above max_inflight it bounds nothing and a backfill starves them.
    history_max_inflight: int = Field(default=8, gt=0)

    # --- Judgement deadline (units/structured_intelligence/pipeline.py) -----
    # One end-to-end budget per judgement, fetch included; past it, 503.
    # PROVISIONAL until Q16: at or below the CRM's own timeout, set with max_inflight.
    # Above the CRM's timeout, the CRM abandons requests we go on to finish.
    judgement_deadline_seconds: float = Field(default=25.0, gt=0)

    # --- Serving behind a proxy (serve.py, register item 94) ---------------
    # The proxies whose X-Forwarded-For/-Proto uvicorn believes (comma list).
    # Loopback only by default, so no forwarded header is trusted by accident;
    # "*" lets any client forge its address and scheme in every log line.
    forwarded_allow_ips: str = Field(default="127.0.0.1", min_length=1)

    # --- Metrics (core/metrics.py, register item 22) ------------------------
    # Whether GET /metrics answers. Off by default: the page is outside the gates,
    # so it must be switched on only where the port is not public; on anywhere
    # public, it tells anyone the service's traffic and failure rates.
    metrics_enabled: bool = False

    # --- Where this runs (core/safety.py) ----------------------------------
    # development, staging or production. development by default, so a laptop
    # and the demo start as ever; production refuses the demo's shortcuts at
    # start, and left at development in production those shortcuts all pass.
    environment: Literal["development", "staging", "production"] = "development"

    # --- Logging (core/logging_config.py) ------------------------------
    # Effective level for the "dodeal_ai" logger tree (audit, error, cost,
    # resilience, validation, ...). Third-party libraries are unaffected --
    # they stay at the root logger's WARNING default.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @model_validator(mode="after")
    def _service_key_is_not_the_user_key(self) -> Settings:
        """An HS256 service secret equal to the user key would let anyone who
        can mint a user token mint a service token too (register item D1).
        Compared as SecretStr, so neither value is read out here."""
        if self.service_jwt_algorithm == "HS256" and self.jwt_signing_key in (
            self.service_jwt_signing_key,
            self.service_jwt_previous_signing_key,
        ):
            raise ValueError("service_jwt_signing_key")
        return self

    @model_validator(mode="after")
    def _stt_request_ends_inside_the_run(self) -> Settings:
        """An STT request must time out while the run can still record it as
        a retry: STT_TIMEOUT_MARGIN_SECONDS or more before the job's timeout."""
        room = self.call_job_timeout_seconds - self.call_stt_timeout_seconds
        if room <= STT_TIMEOUT_MARGIN_SECONDS:
            raise ValueError("call_stt_timeout_seconds")
        return self

    @model_validator(mode="after")
    def _routing_names_resolve(self) -> Settings:
        """Every registry, profile and route name resolves (core/llm/routing.py):
        a profile naming an unknown provider refuses to start. Fixed messages,
        never a name or a value; routing.py checks the pass names."""
        if set(self.llm_providers) & RESERVED_PROVIDER_NAMES:
            raise ValueError("llm_provider_name_reserved")
        if any(
            spec.timeout_seconds > self.llm_timeout_seconds
            for spec in self.llm_providers.values()
        ):
            # Its HTTP timer must fire inside the watchdog's, or a timeout
            # loses the provider's name (openai_compatible._HTTP_TIMEOUT_SHARE).
            raise ValueError("llm_provider_timeout_too_long")
        for name, profile in self.llm_profiles.items():
            if (
                not isinstance(profile.provider, LLMProvider)
                and profile.provider not in self.llm_providers
            ):
                raise ValueError("llm_profile_unknown_provider")
            fallback = profile.fallback_profile
            if fallback is not None and (
                fallback == name or fallback not in self.llm_profiles
            ):
                raise ValueError("llm_fallback_profile_unknown")
        if "default" in self.model_routes:
            raise ValueError("model_route_reserved")
        if "default" in self.call_stt_profiles:
            raise ValueError("stt_profile_reserved")
        for passes in self.model_routes.values():
            if not set(passes.values()) <= set(self.llm_profiles):
                raise ValueError("model_route_unknown_profile")
        return self

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
