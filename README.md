# DODEAL AI

DODEAL AI is the intelligence layer that sits in front of the DODEAL CRM. It is a read-only FastAPI service that will eventually run structured note scoring, call analysis, and an assistant on top of CRM data, without ever holding a direct database connection.

[![CI](https://github.com/DoDeal/AIServicesCrm/actions/workflows/ci.yml/badge.svg)](https://github.com/DoDeal/AIServicesCrm/actions/workflows/ci.yml)

## Table of contents

- [Overview](#overview)
- [Project status](#project-status)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Getting started](#getting-started)
- [Project structure](#project-structure)
- [File reference](#file-reference)
- [Configuration reference](#configuration-reference)
- [Testing](#testing)
- [Code quality and tooling](#code-quality-and-tooling)
- [Continuous integration](#continuous-integration)
- [Unit A — Project 1 (note judgement)](#unit-a--project-1-note-judgement)
- [Security model](#security-model)
- [Documentation index](#documentation-index)
- [Contributing](#contributing)
- [License](#license)

## Overview

DODEAL AI reaches CRM data only through backend tool clients, never through a database connection. Every request that reaches the service passes through a chain of independent security gates before any business logic runs. The service is designed so that unconfirmed integration details with the backend (token format, claim names, response shapes) are isolated behind small, swappable seams and tracked in a dedicated document, rather than guessed at in multiple places.

The current codebase is the foundation, referred to internally as Phase 0. It contains:

- The full gate chain: authentication, tenant isolation, authorization (parked), and cost enforcement.
- Structured audit logging and a fail-closed error boundary.
- A resilience wrapper, an output validator, and a server-side prompt builder.
- A complete tooling and CI setup: type checking, coverage gating, pre-commit hooks, and a GitHub Actions pipeline.

No feature logic ships yet. The feature units (structured intelligence, call intelligence, the assistant, and sales automation) exist only as empty, reserved packages.

## Project status

- The service is read-only. It has no direct database access and performs no writes back to the CRM today.
- Feature units under `src/dodeal_ai/units/` are placeholders. They stay empty until the Phase 0 exit demo criteria are met.
- `src/dodeal_ai/api/routes/_probe.py` is temporary scaffolding used to exercise the gate chain end to end. It is removed before the first real feature route ships.
- Every unconfirmed fact about the backend integration (token type, claim names, role model, tenant indicator, response shapes) is recorded in `ASSUMPTIONS.md`, along with the exact seam to change when the real answer is confirmed.
- RS256 (Gate 1's JWT signing algorithm) is confirmed unrelated to the lead/notes integration, which authenticates with `DD-API-KEY` instead. It is sequenced separately and is not a blocker for the data-layer work.

## Architecture

Every inbound request runs through a small set of independent dependencies, each of which can be tested in isolation:

1. **Gate 1, authentication** (`core/auth/`). Verifies the bearer token and maps it to an internal `Identity`. Fails with a generic 401 on any error.
2. **Gate 2, tenancy** (`core/tenancy.py`). Confirms that the whole `Host` header equals `<tenant>.<inbound_base_domain>` for the tenant carried in the token. Fails with a generic 403 on any mismatch.
3. **Gate 3, authorization** (`core/authz/permissions.py`). Resolves role to permission and enforces default-deny. Currently parked: the token carries no roles yet, so this gate is built and unit-tested but not wired into the live chain.
4. **Gate 4, cost** (`core/cost/limiter.py`). Enforces a per-tenant and per-user request quota backed by Redis. Fails with a generic 429 when a cap is exceeded.

A successful pass through the live gates produces an immutable `RequestContext` (`core/context.py`), which is the single source of tenant and user identity for everything downstream.

Two further layers support the gate chain rather than sitting inside it:

- **Middleware** (`src/dodeal_ai/middleware/`) runs on every request regardless of route: a body-size limit, request-id generation and load shedding today, with logging and timing reserved for later. A request meets them in that order — **body limit, then request id, then inflight, then routing and the gates** — each one cheaper than the one under it and refusing before it. All three are **pure ASGI callables** (`__init__(app)` plus `__call__(scope, receive, send)`), not `BaseHTTPMiddleware` subclasses — register item 86: that base class runs every request through a task group and a pair of memory streams, which is a per-request cost on a load-shed path whose whole claim is that a refusal is a counter comparison, and it runs the route in a child task, so a `contextvars.ContextVar` set in a route never reaches anything outside it. `tests/test_no_base_http_middleware.py` keeps it from coming back.
- **The error boundary** (`core/errors.py`) is the outermost handler. It never touches a deliberate 401, 403, or 429 response; it only catches genuinely unexpected exceptions, logs the real cause internally, and returns a generic 500 to the client.

Two policies apply consistently across the whole gate chain:

- Authentication and tenancy fail closed. An outage in either one risks a data breach, so the safer failure mode is to deny the request.
- The cost gate fails open. An outage there risks a bounded, recoverable, and fully logged amount of unmetered spend, so the safer failure mode is to keep serving requests rather than take the whole service down over a non-critical dependency.

See `docs/architecture.md` for the recorded directory tree and the reasoning behind the build order, and `docs/FUTURE_PATTERNS.md` for patterns that are deliberately deferred to a later phase.

## Requirements

- Python 3.12 or later.
- [uv](https://docs.astral.sh/uv/) for dependency management and running commands.
- Docker, for running Redis locally through `docker-compose.yml`.
- A running Redis instance for the cost gate. The service still starts and serves requests without one; the cost gate fails open and `/ready` reports a degraded state. `/ready` probes the cost connection (db1) and the operational connection (db2) separately and reports each in its own field, so a healthy cost store and a dead idempotency store is a state you can see rather than one flag hiding the other.

## Getting started

1. Install dependencies:

   ```bash
   uv sync
   ```

2. Copy `.env.example` to `.env` and fill in at least the signing key:

   ```bash
   cp .env.example .env
   ```

   ```bash
   DODEAL_JWT_SIGNING_KEY=any-local-value
   ```

   The application fails closed if this variable is missing: it refuses to start, and `/ready` returns `503`. The file must be saved as UTF-8 with no byte order mark. A byte order mark breaks environment variable parsing silently.

3. Start Redis:

   ```bash
   docker compose up -d redis
   ```

4. Run the application:

   ```bash
   uv run uvicorn dodeal_ai.main:app --reload
   ```

5. Run the test suite:

   ```bash
   uv run pytest
   ```

### Running with Docker

The `Dockerfile` builds a non-editable install of the wheel — no source tree in the final image, so this is the same artifact CI verifies with `scripts/verify_wheel.py`. To run the full stack (API + Redis) instead of the local dev flow above:

```bash
docker compose up --build
```

The API reads `.env` (copy `.env.example` first) and talks to the `redis` service's queue and cost databases automatically.

#### The demo

One command brings up Redis and the API against the committed demo runtime (`.env.demo`, register item 78):

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build
```

`.env.demo` is layered **over** `.env`, so the demo's invented tenant, host and signing key win and are the same on every machine, while the three `DODEAL_LLM_*` rows it deliberately leaves unset are whatever a real `.env` supplies. Until a provider, a model and a key are set, `/ready` answers `503 {"llm": "not configured"}` and every judgement route `503`s — that is register item 84 working, not a broken demo.

Then mint a token and run the curl line it prints:

```bash
uv run python scripts/mint_demo_token.py --route probe    # the gate chain alone
uv run python scripts/mint_demo_token.py --route direct   # a judgement, no CRM needed
uv run python scripts/mint_demo_token.py                  # the primary fetch route
```

The `Host` header in that line is the load-bearing part: the connection goes to `localhost:8000`, but Gate 2 only ever reads the header. The `fetch` route additionally needs the fake CRM (register item 79, a separate repository) listening on port **8001** of the Docker host; `docker-compose.demo.yml` maps `tenant-a.crm.demo.invalid` there through `extra_hosts`, and `.invalid` never resolves in DNS, so if that mapping is ever removed the call fails immediately instead of reaching somebody else's host.

See `CONTRIBUTING.md` for the architectural rules and deliberate decisions that apply to any change in this repository.

## Project structure

```
dodeal-ai/
├── .dockerignore
├── .env.example
├── .github/workflows/ci.yml
├── .pre-commit-config.yaml
├── ASSUMPTIONS.md
├── CONTRIBUTING.md
├── Dockerfile
├── docker-compose.yml
├── docs/
│   ├── architecture.md
│   └── FUTURE_PATTERNS.md
├── pyproject.toml
├── scripts/
│   ├── real_fetch_check.py
│   └── verify_wheel.py
├── src/dodeal_ai/
│   ├── main.py
│   ├── api/routes/_probe.py
│   ├── core/
│   │   ├── config.py
│   │   ├── context.py
│   │   ├── errors.py
│   │   ├── log_safety.py
│   │   ├── logging_config.py
│   │   ├── prompting.py
│   │   ├── redis.py
│   │   ├── resilience.py
│   │   ├── tenancy.py
│   │   ├── validation.py
│   │   ├── audit/logger.py
│   │   ├── auth/{claims.py,verify.py,dependencies.py}
│   │   ├── authz/permissions.py
│   │   ├── cost/limiter.py
│   │   └── llm/__init__.py
│   ├── middleware/body_limit.py
│   ├── middleware/inflight.py
│   ├── middleware/request_id.py
│   ├── prompts/
│   │   ├── unit_a_v1.txt
│   │   ├── assistant/
│   │   ├── call_intelligence/
│   │   ├── sales_automation/
│   │   └── structured_intelligence/
│   ├── schemas/
│   │   └── lead.py
│   ├── tools/{leads.py,httpx_transport.py}
│   ├── units/{structured_intelligence,call_intelligence,assistant,sales_automation}/
│   └── workers/runner.py
├── study.py
├── tests/
│   ├── helpers/{tokens.py,test_tokens.py}
│   ├── integration/{fake_backend.py,test_leads_e2e.py}
│   ├── redis_real/{conftest.py,test_scripts_real.py,test_pool_and_breaker_real.py}
│   ├── security/
│   └── unit/
└── uv.lock
```

## File reference

### Root

| File | Purpose |
| --- | --- |
| `pyproject.toml` | Project metadata, runtime dependencies, dev dependency groups, and tool configuration for pytest, coverage, and mypy. |
| `uv.lock` | Locked, reproducible dependency versions resolved by uv. Committed so every environment installs identical packages. |
| `.gitignore` | Excludes the virtual environment, caches, coverage artifacts, editor files, any `.env*` file except `.env.example` and `.env.demo`, and the per-developer Claude Code settings (`.claude/settings.local.json`, `.claude/*.local.*`) while keeping the shared `.claude/settings.json` tracked. |
| `.env` | Local environment variables. Not committed. Must be UTF-8 with no byte order mark. |
| `.env.example` | Documents every `DODEAL_*` setting in `core/config.py::Settings` with its default, or `change-me-local-only` for a secret. Held in step with `Settings` by `tests/test_env_example_matches_settings.py`, which compares key **names** both ways and asserts every secret-shaped row holds a `change-me` placeholder — that last check is what makes the file safe to write without reading it, which is the situation every Claude Code session is in (`.env.*` is a denied read). Grouping is by hand and is not enforced. Copy to `.env` and fill in real local values; never holds real secrets. UTF-8, no BOM, LF. |
| `.env.demo` | **Committed**, and safe to commit: every value in it is invented — no real tenant, no real key, no real host. The demo runtime (register item 78), passed to the container by `docker-compose.demo.yml` as a second `env_file` layered over `.env`. Not a template: `tests/test_env_example_matches_settings.py` pins `.env.example` to `Settings` and is deliberately **not** extended to this file, which sets only what the demo needs to differ from the defaults. It leaves the three `DODEAL_LLM_*` rows unset on purpose — which model the demo runs on is a person's decision and the key is a person's to hold. |
| `docker-compose.demo.yml` | The demo override for `docker-compose.yml`: adds `.env.demo`, resolves the demo's `.invalid` backend host to the Docker host via `extra_hosts` so the fake CRM on the laptop is reachable, and waits for Redis to answer `PING` before starting the API so the first judgement cannot meet a fail-closed idempotency store. A separate file so a plain `docker compose up` is unchanged. |
| `.dockerignore` | Keeps the build context small and secrets out of it: the virtual environment, `.git`, `.env*`, tests, docs, build artifacts, caches, and `study.py`. |
| `Dockerfile` | Multi-stage build. Installs the locked dependencies and the project **non-editable** into a venv, then copies only that venv into a slim runtime image — no source tree in the final image, so this is the same installed-wheel shape `scripts/verify_wheel.py` checks in CI. |
| `.pre-commit-config.yaml` | Local git hooks: ruff lint, ruff format check, and mypy, all run through `uv run` so they use the exact versions locked in `uv.lock`. |
| `docker-compose.yml` | `api` (built from the `Dockerfile`) plus a local Redis 7 container for the cost and queue gates. Its `.env` is `required: false`, so a clean checkout comes up and fails closed in `Settings` with a reason rather than in Compose without one. |
| `ASSUMPTIONS.md` | The single seam ledger for the service. Every provisional decision, organized by status (confirmed, built, parked, pending, deferred), the seam it lives behind, and how to correct it when the real answer lands. |
| `CONTRIBUTING.md` | The contributor guide: local setup, shared vs personal Claude Code permissions, architectural rules that tooling cannot enforce, deliberate decisions not to reverse, branch-protection requirements, the OWASP LLM Top 10 checkpoint habit, and commit style. |
| `.gitattributes` | `* text=auto eol=lf` plus explicit `binary` for `*.png` and `*.pdf`. Line endings are decided here rather than by each developer's `core.autocrlf`. |
| `.python-version` | `3.12`. Pins the interpreter for `uv` locally and, with `uv python install 3.12` in CI, makes both the same. |

### `.claude/`

| File | Purpose |
| --- | --- |
| `settings.json` | **Committed.** The shared Claude Code permission rules, so every contributor's session starts with the same allow / ask / deny list: `uv` and read-only git are allowed, `git push` always asks, and `--amend`, `--force`, `reset --hard`, `rm -rf` and reading `.env` are denied. Personal or machine-specific rules go in `.claude/settings.local.json`, which is gitignored and merged over this file. |

### `.github/workflows/`

| File | Purpose |
| --- | --- |
| `ci.yml` | GitHub Actions pipeline. On every push and pull request: installs uv and a pinned Python 3.12, runs `uv sync --locked`, then the six required checks — ruff check, ruff format check, mypy, pytest with the 92% coverage gate, `scripts/check_coverage_floors.py` for the per-file security floors, and finally a wheel build plus `scripts/verify_wheel.py` to prove an installed copy actually imports. |
| `dependabot.yml` | Weekly dependency updates for two ecosystems: `uv` (all packages grouped into one PR labelled `deps`, because a pinned lockfile means one PR per package would re-run the whole check chain per bump) and `github-actions` (ungrouped — few, and a pinned action bump is worth reading alone). |

### `docs/`

| File | Purpose |
| --- | --- |
| `architecture.md` | The recorded, authoritative directory tree and the build order for Phase 0. Read this before scaffolding anything new, so structure is not reinvented from memory. |
| `FUTURE_PATTERNS.md` | Patterns worth adopting later, each tagged with the phase in which it becomes relevant: idempotency keys, prompt-injection containment for agentic units, the model gateway, prompt caching, async pipeline reliability, evaluation from real examples, observability identifiers, and the OWASP LLM Top 10 checkpoint. Trigger comments in the code point back to specific items here. |
| `STATUS.md` | The build register: where the service is, what is committed and what is next, the audit-finding register with the commit each fix landed in, open questions and who owes what, and the build sequence ahead. Sits beside `ASSUMPTIONS.md` — this file is about the *build*, `ASSUMPTIONS.md` is about the *contract*. Every commit that changes a row updates it in the same commit. |
| `decisions/` | Design notes for decisions taken before the code that depends on them is written. `0001-principal-model-and-execution-model.md` covers the two open before route skeletons: who calls this service and as whom (the principal model), and settling on one execution model instead of three. Both are `PROPOSED`, tracked in `STATUS.md` §2. |
| `runbooks/` | Operational procedures. `secret-rotation.md` covers the JWT verification key, the per-tenant `DD-API-KEY`s and the future provider key: what exists, how each is loaded, why rotation means a deploy (`get_settings()` is `lru_cache`d and `Settings` is frozen — there is no hot reload), the coordinated HS256 window, and the audit-log search terms for establishing blast radius. |

### `scripts/`

| File | Purpose |
| --- | --- |
| `real_fetch_check.py` | A manual, one-shot script for the real, credentialed verification call against the live backend, run by hand for the joint session with the backend team. Not a pytest test and never runs in CI. Dry-runs by default against a guaranteed-unreachable fake host (no real network call); the real call requires an explicit `--live` flag, with a defense-in-depth guard that refuses to target a `dodealcrm.com` host without it. |
| `mint_demo_token.py` | Mints one demo token and prints the `curl` line that uses it, including the `Host` header Gate 2 requires. Makes no network call and never prints the signing key. Reads the signing key, the algorithm, the three claim **names** and the inbound base domain from `Settings`, over the same `.env` → `.env.demo` stack Compose gives the container, so a token it mints cannot be signed with a different key from the one the service verifies against. `--route` picks the curl line: `fetch` (the primary route, needs the fake CRM), `direct` (note in the body, needs no CRM) or `probe` (the gate chain alone). Every default is invented. |
| `check_coverage_floors.py` | Enforces per-**file** coverage minimums for the modules on a deny path (`core/auth/**`, `core/tenancy.py`, `core/cost/**`, `core/errors.py`, `core/validation.py`, `core/log_safety.py`). Reads `coverage.json` written by the pytest run. The repo-wide 92% gate is an average and can be paid for by well-covered code elsewhere; these cannot. Fails closed when a pattern matches no file, so a rename cannot silently drop a floor. stdlib only. |
| `verify_wheel.py` | Installs a built wheel into a throwaway venv and, from a temp directory outside the repo, imports every module under `dodeal_ai` and confirms the packaged prompts directory exists and holds `unit_a_v1.txt`. Runs in CI after the wheel is built; proves an installed copy actually works, not just the editable dev install. |

### `src/dodeal_ai/` (top level)

| File | Purpose |
| --- | --- |
| `main.py` | The FastAPI application instance. Defines the startup lifespan (fail-closed config check, logging configuration, **the nine Unit A prompt templates preloaded before any socket exists** — a missing one refuses startup, register item 85 — then **one pooled `httpx.AsyncClient` and the LLM client built once on `app.state`**, with the template cache cleared and all three released on shutdown), registers the three middlewares (the **last** `add_middleware` call is the outermost layer, so body limit, then request id, then inflight) and the error handlers, and exposes `/health` and `/ready`. **Startup is permissive, readiness is strict:** an unset `DODEAL_LLM_PROVIDER` starts the app, leaves `app.state.llm` as `None` and logs one loud `llm_not_configured` ERROR — the same treatment, and for the same reason, as `backend_keys_missing` — while a provider that IS set and anything else wrong (no key, no adapter, a profile that fails the sweep) is a genuine `ConfigError` and refuses to start. `/ready` returns `503` when configuration is missing **or when no usable LLM client exists**, so a pod that cannot judge never joins rotation; a Redis outage stays `200` with `redis` and `operational` reporting `ok` or `degraded` per connection, because the cost gate fails open and a Redis outage is a state the service serves correctly through. The pool is sized `max_inflight × LLM_CALLS_PER_JUDGEMENT`, against passes rather than requests, because vague and score are gathered. |
| `api/routes/_probe.py` | A temporary route that exercises the full gate chain over HTTP. Used for the Phase 0 exit demo. Scheduled for removal before the first real feature route ships. |

### `src/dodeal_ai/core/`

| File | Purpose |
| --- | --- |
| `config.py` | `Settings`, the single source of runtime configuration, built on `pydantic-settings`. Every module that needs a claim name, a JWT parameter, or a Redis URL reads it from here. The signing key has no default, so a missing key raises a fail-closed `ConfigError` rather than letting the service start unable to verify tokens. |
| `context.py` | `RequestContext`, a frozen dataclass built once the gates have run. It is the single, immutable source of tenant, subject, roles, permissions, and request identity for everything downstream. |
| `tenancy.py` | Gate 2. Requires the whole `Host` header to equal `<tenant>.<inbound_base_domain>` — compared case-insensitively, with a `:port` and one trailing dot tolerated and IPv6 literals rejected — and raises `TenantMismatchError` with `invalid_host` (wrong shape or domain) or `tenant_mismatch` (valid shape, different tenant). Checking the whole host, not just its first label, is what stops `<tenant>.evil.com`. `X-Forwarded-Host` is deliberately not read; the seam is marked in the module docstring. |
| `resilience.py` | The shared watchdog for every external call. Wraps an operation with a timeout and, by default, a single retry on failure. Carries an explicit note that writes must not be retried blindly; a caller wrapping a write should pass `retry=False` or apply an idempotency key. |
| `validation.py` | Validates any tool or LLM output against a Pydantic schema before it is used or returned. Rejects and fails closed on any mismatch, and never logs the raw invalid content. The pydantic `ValidationError` is deliberately dropped rather than chained: it carries the rejected value, so anything that formatted the resulting traceback would print the note text or model output that failed. `OutputValidationError` keeps only the label and one (dotted location, pydantic error type) pair per problem. |
| `prompting.py` | Assembles prompts server-side from versioned files in `src/dodeal_ai/prompts/`. The location is resolved lazily (package default, or `DODEAL_PROMPTS_DIR` override) so importing this module never requires settings to be loaded, and the templates themselves are **read once at startup by the lifespan** (`preload_templates`, register item 85) and held for the app's life, so a judgement makes no disk read on the event loop; `DODEAL_PROMPTS_DIR` still overrides the location. Outside a running app the cache is empty and `_load_template` reads from disk exactly as before, which is what keeps scripts, the wheel check and the tmp-directory tests working. Caller-supplied data is always placed in a clearly delimited section and neutralized against delimiter injection, so untrusted input can never be mistaken for an instruction. The stable system template is placed first and variable caller data last, which is also the shape prompt caching needs once real LLM calls exist. `build_prompt` returns an `AssembledPrompt` (stable template / delimited variable data / reserved tail) whose `.text` is the flat prompt; the split lets a provider adapter place a cache breakpoint without parsing the prompt. |
| `redis.py` | Exposes two named, lazily created Redis clients — cost and quota counters (db1) and per-request operational state (db2) — each with its own **bounded** `BoundedPool` (a `BlockingConnectionPool`) built from the four `DODEAL_REDIS_*` budget settings and sized by `Settings.redis_pool_size`. When a caller cannot get a connection within the acquire timeout, the pool raises `PoolExhausted`, a `redis.ConnectionError` the breaker does not count. Only the acquire is relabelled: a socket that will not connect still raises its own error. There is no numeric literal in the module and a test fails the build if one appears, so every timeout and pool bound has exactly one source of truth. Also exposes `check_cost_redis_ready` and `check_operational_redis_ready`, the two probes `/ready` reports. |
| `breaker.py` | The circuit breaker in front of both Redis connections. `CircuitBreaker` is closed, open, or half-open with exactly one probe, on an injected monotonic clock. `BreakerOpen` subclasses `redis.RedisError`, so every existing failure policy keeps its meaning when a call is refused. `cost_breaker()` and `operational_breaker()` are built from `Settings` on first use. It logs `breaker_opened` / `breaker_closed` on transitions only, plus `breaker_probe_abandoned` when a probe does not answer. A cancelled probe, or one that fails in our own code, re-arms the window. A probe still out after a full window is replaced by the next caller. Either way a probe that never answers cannot leave the breaker half-open for good. `PoolExhausted` is re-raised without being counted, because our own pool running out says nothing about the store; and when it refuses the probe itself the state reverts to OPEN without re-stamping the window, so a busy pool cannot hold the breaker half-open for one. The provider half is not here. |
| `errors.py` | The fail-closed catch-all for unexpected errors. Deliberate `HTTPException` responses (401, 403, 429) pass through untouched; anything else is logged internally at `ERROR` with the request id and returned to the client as a generic 500 with no internal detail. |
| `logging_config.py` | Configures Python logging once at application startup. Emits one structured JSON line per log record to standard output. Sets the `dodeal_ai` logger tree to the configured level so audit `allow` lines are not silently dropped, while leaving third-party libraries at the root logger's default level. |
| `log_safety.py` | The two helpers every exception-logging site uses. `safe_error_fields` returns the exception's type and module always, its message only when the class is defined under `dodeal_ai` (where messages are fixed vocabulary), and a chained exception's class name but never its message. `frames_only` returns the traceback frames without the exception line and without walking the chain. A foreign exception's message is frequently the data that failed, so it is never logged. |

### `src/dodeal_ai/core/auth/`

| File | Purpose |
| --- | --- |
| `claims.py` | The single place that maps internal names to the actual JWT claim keys, and the `Identity` dataclass that carries tenant, subject, database, and roles once verified. |
| `verify.py` | Gate 1's verification logic. Defines the `TokenVerifier` protocol so the verification mechanism can be swapped without touching any caller, and `JwtVerifier`, the current HS256 implementation. Rejects `alg: none` and any algorithm outside the configured allow-list. |
| `dependencies.py` | The gate chain, expressed as FastAPI dependencies: `gate1_identity`, `gate2_tenant`, `build_context`, `require_context`, and `gate4_cost`. Each gate emits a structured audit line on both allow and deny, and converts every failure into a generic HTTP response. |

### `src/dodeal_ai/core/authz/`

| File | Purpose |
| --- | --- |
| `permissions.py` | Gate 3. Resolves a caller's roles into a permission set and enforces default-deny. Built and unit-tested but not wired into the live chain, since the token carries no roles yet and the real role table is unconfirmed. |

### `src/dodeal_ai/core/audit/`

| File | Purpose |
| --- | --- |
| `logger.py` | The structured audit logger. Emits one line per authorization decision with a fixed, safe field set: event, decision, gate, tenant, request id, and reason code. Allow decisions log at `INFO`; deny decisions log at `WARNING` and are never dropped or sampled. |

### `src/dodeal_ai/core/cost/`

| File | Purpose |
| --- | --- |
| `limiter.py` | Gate 4 **and** the token budget, on the same connection and on disjoint keys. Gate 4 counts requests (`cost:*`); the token budget counts what was spent (`tokens:*`). Each pair is moved by its own atomic Lua script, so a Redis failure can never leave one counter of a pair updated and the other not, and an edit to one script cannot reach the other. `token_preflight` **reads** both token keys before a model call and raises `TokenBudgetExceeded` (429) at or above either limit; `enforce_token_cost` **writes** both after a response is in hand, logs `tokens_charged`, and never denies — the call it charges for is already paid. Everything here fails open and logs the bypass if Redis is unreachable, since this is a spend guard rather than a security boundary. Also exposes a read-only `get_usage` for the request counters. |

### `src/dodeal_ai/core/llm/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Re-exports, `build_llm_client(settings, http)` and `get_llm_client(request)`. `build_llm_client` is the real factory: it returns the adapter for `groq` and `openai`, raises `ConfigError` **naming the provider and never a key value** for `anthropic`, for `gemini` (76.3) and for a missing `DODEAL_LLM_API_KEY`, and runs the **startup profile sweep** — every name in `DODEAL_LLM_PROFILES` resolved once through the adapter's own guards, so a bad temperature or a cross-vendor profile refuses to start rather than 503ing the first judgement that names it. `get_llm_client(request)` is the FastAPI dependency and **reads `app.state.llm`, never builds**: one pool per process, and `None` (provider unset) is a fail-closed `LLMConfigurationError`. Gateway concerns (routing, fallback, breakers) grow here, behind the factory. |
| `openai_compatible.py` | `OpenAICompatibleClient` — the adapter for every provider that speaks OpenAI's `/chat/completions`, and `OpenAICompatibleError`, an `LLMProviderError` that also carries the HTTP status and the provider name. Holds the two base-URL constants, the 0–2 temperature bound, the status map (401/403 auth, 429 and 5xx transient, 400 invalid request) and the finish-reason map (`stop`, `length`, everything else `OTHER`). The HTTP call is given a strict **share** of `DODEAL_LLM_TIMEOUT_SECONDS` rather than all of it, so httpx's own timer fires before the watchdog's and a timeout arrives with the provider named. |
| `client.py` | The seam every unit calls a model through: `LLMClient` Protocol (one async `complete()`, prompt passed through unchanged, a required `profile` keyword naming the calling task), frozen `LLMResponse` (OTel-aligned token/finish fields, `text` excluded from repr), `FinishReason`, `LLMProviderError` with enumerated non-interpolated reasons. |
| `profiles.py` | The named model profiles: `PROFILE_UNIT_A_CLASSIFY` / `_VAGUE` / `_SCORE`, the `KNOWN_PROFILES` vocabulary a test greps the unit against, and `resolve_profile(settings, name)` -> `ResolvedProfile`. Holds the fallback rule (an unconfigured name resolves to the `llm_provider`/`llm_model` pair at temperature 0, and no pair either is `LLMConfigurationError`) and the ceiling rule (`effective_max_output_tokens` — a profile may lower a task's ceiling, never raise it). The adapter is what calls it. |

**Two provider classes, not five.** Groq and OpenAI are the *same API* — same path, same bearer auth, same request body, same response shape, same `finish_reason` vocabulary — so one class parameterised by base URL serves both (report R16), and the conformance suite runs every assertion against **both URLs** so a divergence fails a test rather than a deployment. Gemini is a genuinely different shape and gets the second class (item 76.3). **JSON mode is mandatory on every request**, never conditional: Unit A's only use of a reply is `parse_output`, so a fenced or prose-wrapped answer fails validation and spends a **reprompt — a second paid call** — to ask for what the first call could have been told to produce. It is not trust: the reply stays untrusted text until `parse_output` validates it. The adapter **never retries** (a model call is paid and may have completed on the provider's side even when we saw a failure), and its exceptions carry the **status and the provider name only** — never the body, a header, the provider's message, or the base URL, which is itself a credential when it is a proxy override.

### `src/dodeal_ai/middleware/`

| File | Purpose |
| --- | --- |
| `body_limit.py` | The body-size limit, as a **pure ASGI callable** (register item 87), and the **outermost** middleware: a body over `DODEAL_MAX_REQUEST_BODY_BYTES` (64 kB) is refused with **413 `payload_too_large`** before an id is minted, before a slot is taken, before routing and before all four gates. Two paths. A `content-length` above the cap is refused **without calling `receive` at all**, so not one byte comes off the socket — the cheap refusal, and the one an honest client gets. A body that is chunked, carries no length, or **lies** about its length is counted message by message and refused at the byte it passes the cap; the count is an integer, never a copy, so nothing buffers what it is refusing. Exactly the cap is admitted (`>`, not `>=`). It sends both refusals **itself** rather than raising to the `DodealError` handler, because FastAPI re-raises every exception out of `await request.body()` as its own `HTTPException(400, "There was an error parsing the body")` — a delegated refusal would tell the caller its request was malformed when it was only too big; the wrapped `receive` raises to stop the read and the wrapped `send` discards whatever the inner app answered with. The header-path 413 therefore carries `request_id` `"unknown"`, which is the price of refusing before the id exists; the streamed one carries a real id, because by then the id middleware has run. It logs **nothing** — the 413 is the record, and a `WARNING` per oversized request is a log-volume lever a caller controls. `/health` and `/ready` are **not** exempt, unlike in `inflight.py`: they carry no body, so the check costs them nothing, and an exemption is a path a caller can aim a large body at. |
| `inflight.py` | Load shedding, as a **pure ASGI callable** (register item 86). Counts the requests inside the app and refuses the one that meets `DODEAL_MAX_INFLIGHT` immediately with **503 `load_shed`**, through the same `{detail, reason, request_id}` body every enumerated error uses — the refusal `Response` is itself an ASGI app, so it is awaited rather than returned. Installed **inside** the request-id middleware and before everything else, so a refused caller still gets an id to quote while the refusal costs a counter comparison and nothing more — no token verification, no tenant resolution, no body read. The `WARNING` line carries the id and the count and never a tenant (the gates have not run, so the only tenant available would be the caller's own unverified claim) or a body (it was never read). The slot is released in a `finally`, so a route that raises cannot leak one. `/health` and `/ready` are exempt: an orchestrator that cannot reach them under load kills the pod, which is the outage this prevents arriving by another route. A `lifespan` or `websocket` scope passes through uncounted — counted, a lifespan scope would hold its slot until the process exits and the cap would be one lower for the life of the pod. |
| `request_id.py` | Reads an inbound `X-Request-ID` header if it is present **and** well-formed (`[A-Za-z0-9._-]{1,128}`), or generates a new uuid4, and sets it on `request.state.request_id` for every existing consumer to read. A **pure ASGI callable** (register item 86): it writes the two keys into `scope["state"]`, which is the dict `request.state` is a view over, and appends the header to a **copy** of the `http.response.start` message's header list on the way out. The header is caller-controlled and reaches every audit line and the response, so a value that fails the check is discarded exactly as if absent — and never logged, echoed, or reported back. Also stores a `RequestObservability` object for the fuller identifier set (trace id today, prompt version, model version, and workflow version reserved for later) and echoes the id back on the response. Runs inside Starlette's own outermost error-handling middleware but before the gate chain, and passes a `lifespan` or `websocket` scope straight through: a lifespan scope outlives every request in the process, so an id written into it would be one id shared by all of them. |

### `src/dodeal_ai/prompts/`

| File | Purpose |
| --- | --- |
| `unit_a_v1.txt` | A **generic placeholder from Phase 0** — a small, versioned system prompt used to build and exercise the prompt builder itself. It is **not** one of Unit A's nine templates and is not used by the judgement pipeline; it sits one level above `structured_intelligence/` and is deliberately kept, because `scripts/verify_wheel.py` asserts it is present in the installed package (the wheel-packaging check) and `tests/unit/test_prompting.py` builds every one of its prompt-assembly and delimiter-injection tests against it. Do not delete it: removing it breaks the wheel check and four prompting tests. |
| `structured_intelligence/` | **Unit A's nine shipped templates.** `classify_v1.txt`; six per-type vague templates (`vague_no_contact_v1.txt`, `vague_callback_v1.txt`, `vague_discovery_v1.txt`, `vague_viewing_v1.txt`, `vague_negotiation_v1.txt`, `vague_won_lost_v1.txt`); `score_v1.txt`; and `reprompt_tail_v1.txt`, the trusted trailing instruction the single reprompt appends. Seven of the nine carry few-shot examples; `score_v1.txt` deliberately does not, since its examples would have to be marks. No template contains a weight, threshold, band, total, tenant name, host, or credential — asserted per file in `tests/unit/test_scoring.py` and `tests/security/test_unit_a_injection.py`. |
| `assistant/`, `call_intelligence/`, `sales_automation/` | Empty directories reserved for each unit's future versioned prompts. |

So the package ships **ten** prompt files: the nine Unit A templates plus the Phase 0 placeholder above them. Prompts are code: every one is a tracked, versioned file, `build_prompt` is the only assembly path, and there are no inline prompt strings anywhere in `src/`.

Ships inside the package (not a repo-root directory) so an installed wheel has it. `DODEAL_PROMPTS_DIR` overrides the location for local prompt iteration only — see the configuration reference below.

### `src/dodeal_ai/schemas/`

| File | Purpose |
| --- | --- |
| `lead.py` | The confirmed Pydantic response contracts for the lead and note endpoints: `Lead`, `PageMeta`, `LeadListResponse`, `LeadNote`, `LeadNotesResponse`, and `LeadResponse` (single lead; envelope shape unconfirmed, modelled by analogy). Leads and notes are read from `data`, with pagination in `meta`. Unlisted fields from the backend are tolerated rather than rejected, since this is an external response the service does not control. |

Root contracts owned by the backend, kept inside the package for the same reason as `prompts/`. Unit-owned result types live in their unit, not here (`ASSUMPTIONS.md` §3.1).

### `src/dodeal_ai/tools/`

| File | Purpose |
| --- | --- |
| `leads.py` | `LeadsClient`, the only path this service has to lead data. Builds request URLs under `/api/service/...` from the caller's authoritative tenant subdomain, resolves that tenant's key via `TenantKeyResolver` and sends it as the service header, validates each response against the confirmed schema, and runs every call through the resilience watchdog. Exposes `get_leads` (list), `get_lead` (single lead by id), and `get_lead_notes` (a lead's notes). |
| `httpx_transport.py` | The production `httpx`-based transport used by `LeadsClient`. Not exercised by the test suite, which supplies a mock transport instead. |
| `keys.py` | `TenantKeyResolver` Protocol and `SettingsKeyResolver`, the per-tenant DD-API-KEY lookup behind `get_key_resolver()` (same shape as Gate 1's `get_verifier()`). An unknown tenant raises `BackendKeyError` before any network call — never retried, never wrapped by the watchdog. The key is read out of its `SecretStr` in exactly one place: `LeadsClient._headers()`. |

### `src/dodeal_ai/units/`

| Directory | Purpose |
| --- | --- |
| `structured_intelligence/` | Unit A, fast synchronous note scoring. **Built** (campaign phases A–J): `schemas.py`, `config.py` (the TenantConfig seam — the only source of a weight, threshold, cap or TTL), `state.py` (the three db2 concerns), `classify.py`, `vague.py`, `scoring.py`, `decide.py`, `llm_call.py` (the single untrusted-parse boundary), `pipeline.py` (the order the whole unit runs in), and `templates.py` (`UNIT_A_TEMPLATES`, the nine template names the lifespan preloads, built from the constants the calling modules use rather than retyped). Read-only; judgements are returned to the caller, which persists them. |
| `call_intelligence/` | Unit B, slow asynchronous call analysis. Empty. |
| `assistant/` | Unit C1, the conversational assistant. Empty. |
| `sales_automation/` | Unit C2, deferred until after the pilot. Empty. |

### `src/dodeal_ai/workers/`

| File | Purpose |
| --- | --- |
| `runner.py` | The arq worker entrypoint (Decision 2): `WorkerSettings` with Redis derived from `redis_queue_url` and an empty `functions` list. Importing it opens no connection. Step 14 adds the lanes and the real tasks. |

### `tests/` (repo-wide guards)

Six tests that read the tree itself rather than running it:

| File | Purpose |
| --- | --- |
| `test_assumption_markers.py` | Marker reconciliation: every `ASSUMPTION[Qn]` is load-bearing in `src/`, promised in this README, and carries a correction path in `ASSUMPTIONS.md`. The retired step-3 seam marker is asserted **absent** from every tracked file — the token pre-flight has been real since Piece N.2, so a file still carrying the marker is describing a stub that no longer exists. |
| `test_no_sync_clients.py` | Greps every module under `src/dodeal_ai/` for a blocking call in the event loop — `import requests`, `requests.`, `httpx.Client(`, `redis.Redis(`, `redis.StrictRedis(`, `time.sleep(`, `urllib.request` — and fails naming the file and line. This service is one loop: a blocking call does not slow the request that made it, it stops every request in the process, `/health` included. The async spelling of each already exists, so a hit is a habit rather than a necessity. |
| `test_no_base_http_middleware.py` | Greps every module under `src/dodeal_ai/` for the two `starlette.middleware.base` import spellings and for a class that inherits `BaseHTTPMiddleware`, and fails naming the file and line (register item 86). That base class costs a task group and a pair of memory streams **per request**, and runs the route in a child task, which severs `contextvars` between a route and anything outside the middleware. The patterns are import-shaped and class-shaped rather than the bare word, so both middleware docstrings can still explain why they are not one; a self-test asserts the patterns match the spellings they forbid and not the prose. |
| `test_env_example_matches_settings.py` | Compares the `DODEAL_*` key **names** in `.env.example` against `Settings` both ways — a field with no row, and a row with no field — and asserts every secret-shaped row holds a `change-me` placeholder. It never reveals a value; the one test that reads values compares without printing and names only the key that failed, which is what makes the file safe to write without reading it. A commented row counts as documented. |
| `test_demo_env_stack.py` | Pins the `env_file:` order Compose builds across `docker-compose.yml` and `docker-compose.demo.yml` to `scripts/mint_demo_token.py`'s `DEFAULT_ENV_STACK`, exactly and in order. A later env file wins, so a divergence makes the minter sign with a key the service does not verify with — and that surfaces as a generic `401 invalid_token`, indistinguishable from a forged token and reading exactly like a broken gate. Scans the lines rather than parsing YAML, because `pyyaml` is present here only as a transitive dependency and nothing declares it; the scan fails closed, so an unparseable block fails the pin instead of emptying it. |
| `test_hermetic_fakes.py` | Parses every test module with `ast` and fails if a patch of `get_cost_client` or `get_operational_client` hands over anything other than a shared fake from `tests/helpers/` or a `fakeredis` client. Names bound to one, such as `client = FakeCostRedis()` and the fixture that yields it, count as one. Patches of `core/redis.py`'s own factories, in that module's tests, are skipped. |

### `tests/helpers/`

| File | Purpose |
| --- | --- |
| `tokens.py` | Test-only helper for minting signed JWTs with configurable claims, including expired tokens and an `alg: none` token, used across the security test suite. |
| `test_tokens.py` | Unit tests for the token-minting helper itself. |

### `tests/security/`

Tests for the gate chain and everything that enforces it, run over HTTP with `TestClient` and mocked Redis:

| File | Purpose |
| --- | --- |
| `conftest.py` | Shared fixtures that align the runtime signing key and algorithm with the test-token helper's constants. |
| `test_chain.py` | The gate chain end to end: happy path, missing or bad token, cross-tenant host, missing host subdomain, a mixed-case host that is now allowed, a cross-domain host denied with `invalid_host`, the cost cap returning a generic 429 with an audited reason code, and the request-id middleware's behavior (real id in the audit line, a well-formed inbound header honored, an overlong or newline/brace-bearing one replaced by a generated uuid and kept out of the log, id echoed on the response). |
| `test_exit_demo.py` | The Phase 0 exit demo criteria: cross-tenant access blocked and logged, expired and malformed tokens rejected, a valid token passing both live gates, and `alg: none` rejected. |
| `test_audit.py` | The audit logger's level rules, field set, and that a cross-tenant deny actually emits a warning-level line. |
| `test_tenancy.py` | Gate 2's host-matching logic in isolation, without HTTP: a parametrised table covering case-insensitivity, trailing dot, port, a cross-domain host, extra subdomain levels, a bare domain, `localhost`, an IPv6 literal, an absent host, and a different tenant — asserting the reason code on every denial. |
| `test_permissions.py` | Gate 3's role-to-permission resolution and default-deny enforcement, in isolation, kept green even though the gate is not wired into the live chain. |
| `test_verify.py` | Gate 1's token verification: signature, expiry, `alg: none` rejection, and that the confirmed token shape (no `iss` or `aud`) is accepted. Also covers RS256 against an ephemeral test keypair — round-trip, wrong key, and the algorithm-confusion attack — and the clock-skew leeway, including the distinct reason codes for skewed versus forged tokens. |
| `test_errors.py` | The fail-closed catch-all: an unexpected exception returns a generic 500 with no internal detail, while a deliberate `HTTPException` passes through untouched. |
| `test_log_safety.py` | The sentinel tests for audit findings H1, L5, and M9. Each drives a real failure whose data contains a sentinel string, formats the record with the real `JsonFormatter`, and asserts the sentinel is absent from the captured text while the safe structured fields are present: a validation failure through the catch-all, a foreign exception whose message is the sentinel, a chained cause, a watchdog failure, a JSON-looking message that must not forge audit fields, and the audit line's six fields still at the top level at `WARNING`. |

### `tests/unit/`

| File | Purpose |
| --- | --- |
| `test_config.py` | `Settings` defaults, environment variable overrides, immutability, and the fail-closed behavior when the signing key is missing. |
| `test_context.py` | `RequestContext` construction and its frozen, immutable behavior. |
| `test_claims.py` | Claim-to-`Identity` mapping, including the integer `sub` claim normalization, tenant lowercasing, and rejection of malformed tenant claims (`@`, whitespace, dots, leading/trailing hyphen, over-length) with `invalid_tenant_claim`. |
| `test_tenant_label_properties.py` | Hypothesis property tests for the one tenant-label rule at both boundaries: any valid label survives any host decoration (case, trailing dot, port) and comes back lowercased; `tenant_from_host` and `normalise_tenant_label` return either `None` or something matching `TENANT_LABEL_RE` for arbitrary text; and `extract_identity` lowercases any valid label. |
| `test_cost.py` | The cost gate: under and over both caps, tenants counted separately, atomic failure leaving neither counter touched, the `amount` parameter, and the read-only usage function. Uses an in-memory fake Redis. |
| `test_redis.py` | The named Redis client accessors: each reads its own URL, each builds its own bounded `BoundedPool` sized from `Settings` (asserted on the pool's own attributes, with no live Redis), the pool size is `DODEAL_MAX_INFLIGHT` + 4 unless overridden and follows the cap, two concurrent acquires on a pool of one refuse the second as `PoolExhausted` while a socket that will not connect stays a plain `ConnectionError` (both on stub connections that open no socket), both factories stay separate and cached, both readiness probes report their own connection independently, and — the one that keeps the settings honest — the module is parsed and the build fails if any numeric literal has crept back into it. |
| `test_cost_lua.py` | The cost gate's **two** Lua scripts actually executed, against `fakeredis[lua]` in-process (audit M5: neither was previously run by the suite). Per script: both counters move in one `EVAL`; the returned counts are the stored values; the window is set when a counter is created and is **not** refreshed by later additions; a pre-existing key with no TTL never gains one (audit M4, pinned as it behaves today); and at the cap `enforce_cost` denies — after the counter has already moved, because the script increments before Python compares. Both scripts are imported from `limiter.py`, never retyped. The rate limit's db2 script runs here too: it increments only under the limit, refuses at the cap without writing, sets the window on creation and repairs a key with no TTL, and returns `(allowed, count before)`, including through `state.take_rate_limit` end to end. |
| `test_breaker.py` | The breaker's state machine on a fake clock: the threshold opens it; an open breaker refuses without calling; after the window exactly one probe is admitted while it is still in flight, and the rest are refused; probe success closes it and probe failure reopens it for a full window; a success resets the count; a non-Redis error is neither counted nor clears the count; five `PoolExhausted` refusals leave it closed with nothing counted, while five `ConnectionError`s open it; a probe that raises our own error or is cancelled re-arms the window; a probe that never returns is taken over after a window; a probe the pool refuses reverts the state to OPEN with `_opened_at` unchanged, and the very next call — with no clock advance — is admitted as the probe and closes it, while a pool refusal in CLOSED still changes nothing; each transition logs exactly once; and `BreakerOpen` is a `RedisError`. |
| `test_token_cost.py` | The token budget. The counters-never-touch test (one scored judgement through the route moves `cost:*` by one and `tokens:*` by the summed usage, and the two scripts' key sets are disjoint); the pre-flight at the limit as a 429 with the exact body and **zero** model calls, one under allowing, and a dead Redis allowing with the bypass logged on every call; an open cost breaker bypassing both the pre-flight and the charge with `breaker: open` on the line and no `MGET` or `EVAL` reaching the store; a charge that fails leaving the judgement complete; the charge count per outcome (three scored, four reprompted, zero suppressed); and the warning firing exactly once on the crossing and not on the call after it. |
| `test_resilience.py` | The watchdog: success on the first attempt, success on retry, failing closed after retry, honoring `retry=False`, and respecting the timeout. |
| `test_lead_schema.py` | The lead and note response schemas: parsing the `data`-wrapped shape, the single-lead and notes envelopes, and failing closed on a malformed one. |
| `test_validation.py` | `validate_output`'s failure path: `OutputValidationError` carries only (dotted location, pydantic error type) pairs, the pydantic error is not chained, and no attribute holds the rejected input. |
| `test_keys.py` | `SettingsKeyResolver`: a known tenant resolves to its configured secret, an unknown tenant raises `BackendKeyError` with no key material (from that tenant or any other) in the exception or the log line, and `SettingsKeyResolver` satisfies the `TenantKeyResolver` Protocol. |
| `test_leads_client.py` | `LeadsClient`: URL construction under `/api/service/...` from the tenant subdomain, that each tenant's request carries *that tenant's own* key (the F2 isolation test), failing closed with zero transport calls and an unwrapped `BackendKeyError` for an unknown tenant, `get_leads`/`get_lead`/`get_lead_notes` reading `data`, and failing closed on a malformed response. |
| `test_prompting.py` | The prompt builder: server-side assembly and that caller-supplied data can never become an instruction. |
| `test_assembled_prompt.py` | `AssembledPrompt`: `.text` byte-identical to the pre-structure output, untrusted text never in `.stable`, tail rendered after the data, `.variable` excluded from repr. |
| `test_llm_seam.py` | The LLM seam: frozen `LLMResponse` with repr-safe text, enumerated `LLMProviderError` messages, `DODEAL_LLM_*` settings and fail-closed provider validation, the factory's configuration errors, the runtime-checkable Protocol, the prompt-type pass-through contract, and that `profile` is a required keyword on a Protocol that still has exactly one method. |
| `test_startup.py` | The startup signal and the LLM client lifespan builds: `backend_keys_missing` on an empty key map; `llm_not_configured` at ERROR on the startup logger with `app.state.llm` left `None`; one client built and closed on shutdown; the pool sized from `max_inflight`; a configured-but-broken provider (no key, no adapter, no model) refusing to start; and the startup profile sweep refusing a cross-vendor profile **without interpolating its model id** while accepting one a real call would accept. |
| `test_openai_compatible_adapter.py` | The OpenAI-compatible adapter on `httpx.MockTransport`, every assertion parametrised over **both** base URLs: JSON mode, the profile's temperature and model, the ceiling rule in both directions, one user message carrying `stable`/`variable`/`tail` in order; the key in the bearer header and in no body, URL or log line; `stop`/`length`/other finish reasons; 400, 401, 403, 429, 500 and 503 each to their reason and transience with the status kept and the body's sentinel absent from `str`, `repr` and every log record; a timeout raising the seam's transient error and never a builtin `TimeoutError`; non-JSON and each missing field transient; the transport entered **exactly once** on every failure; a temperature over the bound and a profile naming another vendor both failing at resolution before the transport is entered; and `build_llm_client` refusing `anthropic`, `gemini` and a missing key by name. |
| `test_model_profiles.py` | Model profiles: a configured profile resolving to its own provider, model and temperature; an unconfigured name falling back to the single-model pair at temperature 0; no profile and no pair raising `LLMConfigurationError` with the fixed `llm_not_configured`; malformed JSON, an empty model, an unknown provider and an out-of-range temperature each failing at settings construction; the ceiling rule in both directions; and a grep over `src/dodeal_ai/units/` asserting every `profile=` names a `KNOWN_PROFILES` value. |
| `test_logging_config.py` | The structured logging setup: an allow line actually reaching standard output under the real configuration, a deny line at warning level, the cost-bypass warning reaching the same stream, third-party loggers staying quiet, and the configuration being called from the application lifespan. |
| `test_inflight.py` | Load shedding: a request below the cap passes and the counter returns to zero, a route that raises still gives its slot back, the request at the cap gets `503 load_shed` with the id echoed in the body and the header, the `WARNING` line carries the id and the count and no tenant, a refused request's body reaches no log line, `/health` and `/ready` bypass the cap without taking a slot, and — the one that matters — twenty concurrent requests against a stub slow route through an ASGI transport are admitted **exactly** to the cap and refused exactly beyond it, which is what distinguishes a correct counter from one that merely sheds something. |
| `test_body_limit.py` | The body-size limit (register item 87). Raw-ASGI tests for the claims about the middleware alone: a declared length over the cap refuses with `receive` **never called** and the inner app never entered, a streamed body is counted across messages and stops being read at the byte it overflows, a lying or unparseable `content-length` buys nothing, the first header wins, exactly the cap is admitted, and a `lifespan` or `websocket` scope is untouched. `TestClient` tests through the real app at the real cap for the claims about the whole stack: 64 kB exactly is judged, 64 kB + 1 is 413, an oversized body with **no token at all** is 413 and not 401 (the ordering claim), nothing is fetched and no model is called, and a **chunked** body is refused with 413 rather than with FastAPI's own 400 — the assertion that would catch a future rewrite delegating the refusal. |
| `test_middleware_asgi.py` | What the pure-ASGI shape buys and what it must not break (register item 86). A `ContextVar` set **in the route** is visible to a middleware outside both of ours — the direction a task boundary severs, and the one that discriminates: downward propagation worked before the rewrite too, because a child task copies its parent's context at spawn. The route runs in the **same asyncio task** as the middleware. A `lifespan` and a `websocket` scope reach the inner app as the same object, with nothing added and no slot taken, while an `http` scope on the same stack is counted and identified — the control that would catch a pass-through swallowing real requests. And the id header is appended to a **copy**, so an inner app's own header list is never grown. |
| `test_health.py` | `/health` and `/ready`: config missing returns 503, Redis down returns 200 with a degraded body, and Redis up returns 200 with an ok body. |
| `test_startup.py` | The application lifespan logs `backend_keys_missing` at `ERROR` when `DODEAL_DD_API_KEYS` is empty, and does not when it holds at least one tenant. Never refuses to start either way — the gate chain and `/ready` must work before a key is provisioned. |

### `tests/integration/`

Excluded from the default test run (see Testing below).

| File | Purpose |
| --- | --- |
| `fake_backend.py` | A small FastAPI app standing in for the real backend: serves the confirmed lead/note shapes at the real paths and enforces `DD-API-KEY`, returning 401 without it. Test fixture code, not production code. |
| `test_leads_e2e.py` | Runs `LeadsClient` through the real `HttpxTransport` (not the mocked transport the hermetic suite uses), wired to `fake_backend.py` via `httpx.ASGITransport` so no real socket, DNS, or TLS is involved. Proves the real HTTP code path end to end: leads parsed, the API key sent, empty notes handled as a valid result, and a malformed response failing closed. |

### `tests/redis_real/`

The real-Redis lane. It is excluded from the default run (see "The real-Redis lane" under Testing below). Each test names its hermetic counterpart in its docstring.

| File | Purpose |
| --- | --- |
| `conftest.py` | Marks every test in the directory `redis_real` by path, so a module that forgets its own marker still stays out of the default run. Reads `DODEAL_REDIS_REAL_URL`, builds one `redis.asyncio` client with Settings' default socket timeouts and pings it once. It skips every test when the variable is unset, when `PING` fails (the reason names the error type), or when the URL selects db0, db1 or db2. It gives each test a uuid key prefix and deletes every key under it with `SCAN` on the way out, then checks none are left. The lane runs on one session event loop, because a `redis.asyncio` connection belongs to the loop that opened it. |
| `test_scripts_real.py` | The three Lua scripts and the reservation, on a real server. It uses the imported scripts, key spellings and stored values, never retyped copies. **Request and token scripts:** both counters move together; the window is set on create and not refreshed; a key without a TTL never gains one (M4, pinned as it behaves today, on both scripts); and neither namespace moves the other. **Rate-limit script:** `[1, count before]` up to the limit and `[0, count]` at it; the window is set on create and repaired on a key with none; and 15 concurrent `EVAL`s against a limit of 10 take exactly 10 slots, each seeing a different count. **Reservation:** `SET NX EX` claims once, and has one winner among ten concurrent claims. `SET XX EX` creates nothing on a missing key and replaces both value and TTL on a held one. `DEL` frees the note for a new claim. |
| `test_pool_and_breaker_real.py` | A `BoundedPool` of one, holding a real connection, refuses a second acquire as `PoolExhausted`, with redis-py's own cause chain (`ConnectionError` from `TimeoutError`), and recovers once the connection is handed back. A dead host (the lane's host on a port nothing listens on) fails as a store error that is **not** `PoolExhausted`. A breaker at a threshold of two opens on it and then refuses without calling the factory, inside the connect budget. A live host keeps the breaker closed and clears a failure count. |

## Configuration reference

All configuration is read through `Settings` in `core/config.py`. Every variable uses the `DODEAL_` prefix. The signing key is the only variable with no default.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DODEAL_JWT_SIGNING_KEY` | none, required | The key inbound tokens are verified against. Under `HS256` this is the shared secret; under `RS256` it holds the CRM's **public** key (PEM). The name is kept for env stability. The service refuses to start without it. |
| `DODEAL_JWT_ALGORITHM` | `HS256` | The JWT algorithm. Explicitly allow-listed so an `alg: none` token — or an `HS256` token under an `RS256` config — is always rejected. |
| `DODEAL_JWT_LEEWAY_SECONDS` | `30` | Clock-skew tolerance applied to `exp`, `nbf`, and `iat`. The CRM mints tokens on its own clock; without leeway a few seconds of drift rejects valid tokens. Do not set to `0` in production. |
| `DODEAL_CLAIM_SUBJECT` | `sub` | The wire claim name mapped to the internal subject (user id). |
| `DODEAL_CLAIM_SUBDOMAIN` | `subdomain` | The wire claim name mapped to the internal tenant identifier. |
| `DODEAL_CLAIM_DATABASE` | `database` | The wire claim name mapped to the tenant's database name, carried for the tool layer. |
| `DODEAL_DD_API_KEYS` | `{}` (empty map) | JSON map of tenant subdomain to that tenant's DD-API-KEY, e.g. `{"tenant-a":"<key>","tenant-b":"<key>"}`. No default value exists for any tenant; an unknown tenant fails closed in `tools/keys.py` before any network call. An empty map means nothing can reach the backend, and is logged at `ERROR` on startup. |
| `DODEAL_BACKEND_BASE_DOMAIN` | `dodealcrm.com` | The base domain used to build a tenant's **outbound** backend URL. |
| `DODEAL_BACKEND_SCHEME` | `https` | The scheme that same outbound URL is built with. `https` or `http`, nothing else — any other value is a `ConfigError` when `Settings` is built. **`http` is demo-only** and is set in exactly one committed file, `.env.demo`: a laptop serving the fake CRM has no certificate for `tenant-a.dodealcrm.com`. It does **not** disable certificate verification, and no setting does. Set to `http` in production it puts the per-tenant `DD-API-KEY` on the wire in clear on every fetch — see ASSUMPTIONS.md §3.9, `DECISION[DEMO_SCHEME]`. Set to `http`, startup logs `backend_scheme_insecure` at **ERROR** from `dodeal_ai.startup` and serves anyway: the demo needs `http`, so it is a loud line and never a refusal. |
| `DODEAL_INBOUND_BASE_DOMAIN` | `dodealcrm.com` | The base domain requests to this service arrive under. Gate 2 requires `Host == <tenant>.<inbound_base_domain>`. Kept separate from the outbound domain because the host of arrival is an open question with the backend and may become e.g. `ai.dodealcrm.com` independently. |
| `DODEAL_PROMPTS_DIR` | none | Overrides where prompt templates are read from. Unset uses the copies shipped inside the package (`src/dodeal_ai/prompts/`); set only for local prompt iteration without a rebuild. A template named by Unit A and **missing from this directory refuses startup** (register item 85): all nine are read before any socket is opened, so a missing file is a refusal, not a 503 on the first paid call. |
| `DODEAL_EXTERNAL_CALL_TIMEOUT_SECONDS` | `10.0` | The timeout applied to every external call by the resilience watchdog. |
| `DODEAL_EXTERNAL_CALL_RETRY_ONCE` | `true` | Whether the watchdog retries once by default. Individual callers can override this per call. |
| `DODEAL_LLM_PROVIDER` | none | Which model adapter the factory builds: `groq`, `openai`, `anthropic` or `gemini`. Unset means not configured and the factory refuses; `anthropic` and `gemini` parse but have no adapter yet (76.3) and are refused **by name**, so a deployment is told which half is missing. |
| `DODEAL_LLM_API_KEY` | none, refused | The provider API key, a `SecretStr` with no placeholder. Read in exactly one place — the adapter's bearer-header builder. Absent is a `ConfigError` naming the provider, never the key. |
| `DODEAL_LLM_BASE_URL` | none | Proxy override for the provider base URL. Unset uses the provider constant (`https://api.groq.com/openai/v1`, `https://api.openai.com/v1`). Set it only to put a proxy in front — it is never logged and never carried on an exception, because a proxy URL's host or path can itself be the credential. |
| `DODEAL_LLM_MODEL` | empty, refused | The exact pinned model id, set per deployment. No drifting default. |
| `DODEAL_LLM_TIMEOUT_SECONDS` | `60.0` | Per-call timeout for a model call, passed into the watchdog with `retry=False`. Deliberately separate from the 10s external-call timeout. **It is the outer bound:** the HTTP call itself gets a strict share of it (`_HTTP_TIMEOUT_SHARE`), so httpx gives up first and the failure names the provider instead of arriving as a bare `TimeoutError`. |
| `DODEAL_LLM_MAX_OUTPUT_TOKENS` | `1024` | Default output ceiling, sized with headroom for Arabic. |
| `DODEAL_LLM_PROFILES` | `{}` (empty map) | JSON map of profile name to that task's model choice, e.g. `{"unit_a.classify":{"provider":"anthropic","model":"<id>","temperature":0},"unit_a.vague":{"provider":"anthropic","model":"<id>"},"unit_a.score":{"provider":"anthropic","model":"<id>"}}`. A profile may carry `temperature` (0–1) and `max_output_tokens`. **The fallback rule:** a profile name that is not in this map resolves to the `DODEAL_LLM_PROVIDER` / `DODEAL_LLM_MODEL` pair at temperature 0, so a single-model deployment configures that pair and writes no profiles at all. A profile's `max_output_tokens` may only **lower** a task's ceiling, never raise it. Every value is validated when settings are built: a malformed map, an empty `model`, an unknown provider or a temperature outside 0–1 refuses to start. |
| `DODEAL_REDIS_QUEUE_URL` | `redis://localhost:6379/0` | The work-queue Redis connection, read by the arq worker (`workers/runner.py`). |
| `DODEAL_REDIS_COST_URL` | `redis://localhost:6379/1` | The cost and quota Redis connection used by Gate 4. |
| `DODEAL_REDIS_OPERATIONAL_URL` | `redis://localhost:6379/2` | The operational Redis connection used by the feature units for idempotency reservations, clarification rate limits and per-note attempt counters. A separate logical DB from the cost connection because the failure policies differ: losing the cost store fails open, losing the idempotency store fails closed. |
| `DODEAL_REDIS_CONNECT_TIMEOUT_SECONDS` | `0.25` | How long a connection attempt to either Redis may take. Deliberately far shorter than the read timeout: reaching a listening socket on the same network is a sub-millisecond operation, so a slow connect means the host is gone rather than busy. **Provisional** — sized against a same-network Redis, not measured. Must be positive; `0` is refused at startup. |
| `DODEAL_REDIS_SOCKET_TIMEOUT_SECONDS` | `1.0` | How long a single Redis command may take, including the `/ready` `PING`. This is the number that bounds what a Redis outage costs a request: the cost gate fails open after it elapses, so the request continues rather than hanging. **Provisional.** Must be positive. |
| `DODEAL_REDIS_MAX_CONNECTIONS` | unset (= `DODEAL_MAX_INFLIGHT` + 4, so `36`) | An optional override of each connection pool's size. There is one pool per named connection, not one shared. Unset, the size is **derived**: one connection per request the in-flight cap admits, plus 4 of headroom for the `/ready` pings the cap does not count. Raising the cap raises the pool with it. A value below `DODEAL_MAX_INFLIGHT` lets load queue on the pool and be refused (`PoolExhausted`). That call's own policy still applies, so the reservation returns `503`, but the breaker does not count it (register item 81). The pool is **bounded** either way: an unbounded pool answers a Redis stall by opening more sockets, which turns one slow dependency into file-descriptor exhaustion. **Provisional.** Must be positive when set. |
| `DODEAL_REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS` | `1.0` | How long a caller waits for a free connection once the pool is at its cap, before the pool refuses. Without it, "bounded" would mean "blocks forever at the cap", which is a worse outage than the one the bound prevents. **Provisional.** Must be positive. |
| `DODEAL_BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive Redis failures that open a connection's circuit breaker (`core/breaker.py`). There is one breaker per connection, so a dead cost store cannot refuse the idempotency reservation. While open, calls are refused without touching a socket: the fail-open paths bypass as they would on a timeout, and the reservation still returns `503 idempotency_unavailable`. **Provisional.** Must be positive. |
| `DODEAL_BREAKER_OPEN_SECONDS` | `30.0` | How long an open breaker refuses before it lets **one** probe through. The probe's result alone decides whether it closes or reopens for another full window. Measured on the monotonic clock. **Provisional.** Must be positive. |
| `DODEAL_COST_PER_TENANT_LIMIT` | `10000` | The per-tenant request cap per window. |
| `DODEAL_COST_PER_USER_LIMIT` | `1000` | The per-user request cap per window. |
| `DODEAL_COST_WINDOW_SECONDS` | `86400` | The cost counter window, in seconds. Shared by the request counters and the token counters. |
| `DODEAL_COST_TOKENS_PER_TENANT_LIMIT` | `5000000` | The per-tenant **token** budget per window, on its own `tokens:*` keys. A judgement whose tenant is at or above it is refused before the first model call with **429 `token_budget_exceeded`**. **Provisional** — no real provider has run, so this is a placeholder, not a measurement. Must be positive. |
| `DODEAL_COST_TOKENS_PER_USER_LIMIT` | `500000` | The per-user token budget per window, keyed on the verified `sub`. Same refusal, same code. **Provisional.** Must be positive. |
| `DODEAL_COST_TOKEN_WARNING_RATIO` | `0.9` | The fraction of either limit at which a running total earns one `token_budget_warning` line — once per crossing, not once per call. Strictly between `0` and `1`: `0` would warn on the first token and `1` only once the budget was already spent. It changes **nothing** about what is served; degradation at the ratio is a separate, unmade decision. |
| `DODEAL_MAX_INFLIGHT` | `32` | How many requests may be inside the app at once. The next one is refused immediately with **503 `load_shed`** rather than queued behind work the event loop cannot get to. Must be positive; `0` is refused at startup. **Provisional** — a placeholder chosen to be obviously a placeholder, not a measurement. The load lane sets the real number, so treat a `load_shed` line today as "this number is wrong" rather than as capacity. |
| `DODEAL_MAX_REQUEST_BODY_BYTES` | `65536` | The largest request body the app will read. Above it the request is refused with **413 `payload_too_large`** at the outermost middleware, before any gate runs. 64 kB is four times the largest real note plus its lead fields: a **memory bound, not a content rule** — a body under the cap is not thereby valid, and the schemas still decide that. Must be at least 1; too small a value makes every judgement a 413, an outage that looks like the CRM sending bad requests. It does **not** replace the edge limit (register item 43, DevOps): bytes should be refused where they are first accepted, and this is the bound that holds when the edge has none. |
| `DODEAL_JUDGEMENT_DEADLINE_SECONDS` | `25.0` | One end-to-end budget per judgement, from the entry point, so the fetch route's two backend reads are inside it. Past it the request answers **503 `judgement_deadline_exceeded`** and logs a `WARNING` line of the same name carrying `tenant`, `request_id`, `elapsed_ms` and `inflight`. The per-call timeouts are unchanged; this bounds their sum, which is otherwise about 280 s while the CRM waits inline. **Provisional until Q16:** it must be at or below the CRM's own timeout on its call to us. Above that, the CRM abandons requests we go on to finish. The load lane sets it together with `DODEAL_MAX_INFLIGHT`. Must be positive. |
| `DODEAL_LOG_LEVEL` | `INFO` | The effective level for the `dodeal_ai` logger tree. Third-party libraries are unaffected. |

`scripts/real_fetch_check.py` (a manual tool, not part of the test suite) also reads `DODEAL_CHECK_TENANT`, a plain environment variable rather than a `Settings` field, as an alternative to passing the tenant subdomain as a command-line argument.

## Testing

- Run the full suite with `uv run pytest`. Coverage runs by default and the build fails if total coverage drops below the floor configured in `pyproject.toml`.
- The default run is fully hermetic: no test opens a live Redis connection, makes a network call, or calls an LLM. Redis is mocked or faked in every test; the shared fakes in `tests/helpers/` (`FakeCostRedis`, `FakeOperationalRedis`) are the pattern for a new test that needs Redis behavior (`tests/test_hermetic_fakes.py` fails the build on a locally written one), and `fakeredis[lua]` (`tests/unit/test_cost_lua.py`) is the one for a test that needs Redis to actually execute something — Lua included. It is hermetic with or without a Redis listening on the machine: the root `tests/conftest.py` hands every test the shared fakes, so there is no need to stop the compose Redis first, and only the `redis_real` lane below needs one. **It is hermetic against a populated `.env` in the same way** (register item 115): `_no_dotenv` in the root conftest clears `env_file` for the whole session, so the suite is green whether or not you have configured a local one. That was not true until 115 — configuring `.env` to make a live provider call turned six tests red, and for a **dict** setting such as `DODEAL_DD_API_KEYS` pydantic-settings *merges* the file into what a test set by hand, so no `monkeypatch.setenv` could have defended it. A test that genuinely wants a file passes `_build_settings(_env_file=...)`, which is untouched.
- Beside the suite, the **dependency audit** checks the locked dependency set against the advisory database: `uv export --format requirements-txt --no-emit-project -o audit.txt` then `uv run pip-audit --strict --desc -r audit.txt`. It is not part of the stopping chain and runs as its own CI job (see "Continuous integration"); on Windows set `PYTHONIOENCODING=utf-8` first.
- Security-focused tests live under `tests/security/` and exercise the gate chain over HTTP with `TestClient`. Everything else lives under `tests/unit/`.
- `tests/integration/` is excluded from the default run via a registered `integration` marker (`pyproject.toml`), so it stays out of `uv run pytest` and CI. Run it explicitly with `uv run pytest -m integration --no-cov` (`--no-cov`: the coverage gate is sized for the full hermetic suite, not this handful of tests).

- `tests/redis_real/` is excluded from the default run the same way, by the `redis_real` marker: it is the third Redis lane, for a test that needs a **genuine** server. See "The real-Redis lane" below.
- `scripts/real_fetch_check.py` is a separate, manual, non-pytest script for the one real credentialed call to the live backend, used for joint verification with the backend team. It dry-runs against a guaranteed-unreachable fake host by default; the real call requires an explicit `--live` flag. See the script's own docstring for usage.

### The real-Redis lane

The default run exercises Redis on fakes only: the shared fakes in `tests/helpers/`, and `fakeredis[lua]`, which runs real Lua in-process. `tests/redis_real/` runs the same behaviours against a real Redis server, which is the only place they meet a real network, a real clock and real concurrency. It shows the following:

- **The three Lua scripts.**
  - The request and token counters move together.
  - Their windows are set on create and never refreshed.
  - A counter without a TTL never gains one. That is audit M4, pinned as it behaves today.
  - The two namespaces never touch.
  - The rate limit takes exactly `limit` slots when its `EVAL`s arrive at once.
- **The reservation.**
  - `SET NX EX` has one winner, even among concurrent claims.
  - `SET XX EX` never creates a key, and replaces value and TTL on a held one.
  - `DEL` frees the note.
- **The pool.** A `BoundedPool` at its cap refuses as `PoolExhausted`, through the exact cause chain `BoundedPool` relies on. A dead host fails as a store error that is not `PoolExhausted`: a `ConnectionError`, or a `TimeoutError` where the connect outlives its 0.25 s budget first (a Windows loopback does, and so does a firewall that drops rather than refuses).
- **The breaker.** It opens on a dead host, then refuses without touching a socket. A live host keeps it closed.

Start Redis, point the lane at database 9, and run it.

PowerShell:

```powershell
docker compose up -d redis
$env:DODEAL_REDIS_REAL_URL="redis://localhost:6379/9"
uv run pytest -m redis_real --no-cov
```

bash:

```bash
docker compose up -d redis
export DODEAL_REDIS_REAL_URL=redis://localhost:6379/9
uv run pytest -m redis_real --no-cov
```

- **Database 9** is chosen so the lane never touches db0 to db2, the queue, cost and operational stores the service itself uses. The lane enforces this rather than trusting it: a URL that selects db0, db1 or db2 skips every test. Every key sits under a per-test uuid prefix and is deleted with `SCAN`, never `KEYS`, when the test ends.
- **Skipped, never failed, without a server.** An unset `DODEAL_REDIS_REAL_URL`, or a server that does not answer `PING`, skips every test, and the reason names the variable (and the error type).
- **`--no-cov`**, because the coverage gate is sized for the full default run.
- **A hand run, not part of the default chain.** `uv run pytest` and CI deselect the marker. Run the lane by hand before any milestone that puts a real note through the service. It last ran in Piece N.4, against Redis 7.4.10: 21 passed.

## Code quality and tooling

- **Linting and formatting**: [ruff](https://docs.astral.sh/ruff/), run as `uv run ruff check .` and `uv run ruff format --check .`.
- **Type checking**: [mypy](https://mypy-lang.org/), scoped to `src/`, configured with the `pydantic.mypy` plugin for accurate model inference. Run as `uv run mypy`.
- **Pre-commit hooks**: defined in `.pre-commit-config.yaml`. All three checks above run on every commit through `uv run`, so they always use the exact versions locked in `uv.lock`. Install the hooks locally with:

  ```bash
  uv run pre-commit install
  ```

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request, as **two jobs**: `checks`, the required chain below, and `audit`, the dependency audit. Both install uv and a pinned Python 3.12, then run `uv sync --locked` to catch a lockfile that has drifted from `pyproject.toml`.

**The required checks**, in order. Every one must be green before a branch merges; branch protection is configured in the repository settings, not in the tree (see `CONTRIBUTING.md`, "Branch protection"):

| # | Check | Command |
| --- | --- | --- |
| 1 | Lint | `uv run ruff check .` |
| 2 | Format | `uv run ruff format --check .` |
| 3 | Types | `uv run mypy` |
| 4 | Tests + total coverage gate (92%) | `uv run pytest` |
| 5 | Per-file coverage floors | `uv run python scripts/check_coverage_floors.py` |
| 6 | Wheel build + install/import check | `uv build --wheel` then `uv run python scripts/verify_wheel.py dist/*.whl` |

Checks 1-4 are the same ones run locally by the stopping chain and by the pre-commit hooks. Check 5 exists because the 92% gate is an average and can hide one security module rotting; check 6 is the only one that tests the artifact a deploy receives rather than the source tree.

**The `audit` job** is deliberately not one of the six. It runs `uv export --format requirements-txt --no-emit-project -o audit.txt` then `uv run pip-audit --strict --desc -r audit.txt`, and fails on any finding — there is no `continue-on-error` and no `--ignore-vuln`. It sits in its own job because its result is a function of the advisory database rather than of the commit: a newly published advisory should be able to go red without turning the required chain red on a hotfix that changed no dependency. It audits the **exported lock, never the venv** (ruling R38) — the editable project is not on PyPI, can never be resolved, and `--strict` counts that skip as a failure, which is why `--no-emit-project` is not optional here. A finding is fixed by a pin change in its own commit.

The repo-wide guards are not separate jobs — they are ordinary pytest tests and so run inside check 4. `tests/test_no_sync_clients.py` fails the build if a blocking client or a `time.sleep` appears anywhere under `src/dodeal_ai/`, `tests/test_no_base_http_middleware.py` fails it if a `BaseHTTPMiddleware` returns to `src/`, and `tests/test_assumption_markers.py` fails it if a provisional-answer marker stops appearing in the code, this README and `ASSUMPTIONS.md` together.

## Unit A — Project 1 (note judgement)

> **No provider has been CALLED yet.** The Groq/OpenAI adapter exists (76.1) and lifespan now builds it once and hands it to `get_llm_client()` (76.2, item 84), so the wiring is complete and the factory has **no test switch** — one was forbidden for the whole build and remains forbidden. `FakeLLM` is injected through `app.dependency_overrides` and is the only model any of this has run against. **The token pre-flight is real as of Piece N.2**: `core/cost/limiter.py::token_preflight` reads the token counters before the first model call and the pipeline enforces the answer, so the step-3 stub and its grep marker are gone. Nothing in this unit is marked `[V]` — see `ASSUMPTIONS.md` §8.11.

### Provisional answers

Five questions were answered provisionally so the unit could be built. Each is marked in the code with a greppable token, so the blast radius of a wrong answer is `grep -r "ASSUMPTION\[Qn\]" src/`.

| # | Assumed | If wrong | Grep marker |
| --- | --- | --- | --- |
| **Q1** | The CRM forwards the **end user's JWT** and tenant Host, so the judgement route runs Gate 1 → Gate 2 → Gate 4 exactly as the probe route does. | The principal source swaps behind the D1 seam. `judge_note` takes a `TenantScope`, not a `RequestContext`, so the pipeline does not change — only what builds the scope. A service principal would collapse every salesperson into one rate-limit bucket; that is the first line to re-read. | `ASSUMPTION[Q1]` |
| **Q6** | `/leads/{id}/notes` **may** return timeline events as well as notes, so `system_event` is a real note type: decided first, suppressed `not_scorable`, never vague-checked, never scored. | Nothing to undo — the type costs one enum member and a branch that never fires. We do not need the answer to be correct, only to know how often it happens. | `ASSUMPTION[Q6]` |
| **Q7** | The JWT `sub` and a note's `author_id` are **different id spaces**, and nothing joins them. The rate limit keys on the verified `sub`; the judgement reports `author_id` as the backend gave it. | If they match, nothing breaks — the join simply becomes possible, which is what per-rep coaching over time would need. If they differ, the correction is a mapping table and it is the backend's to provide. | `ASSUMPTION[Q7]` |
| **Q8** | The note being judged is on **page one** of the lead's notes (newest first, 25 per page), so one un-paged fetch finds it. | The note is matched **by id**, never by position, so a miss is a clean `404 note_not_found` — never the wrong note. The fix is query parameters in `tools/leads.py` at **step 4**, not a pipeline change. Watch for a rise in `note_not_found`. | `ASSUMPTION[Q8]` |
| **Q13** | **No lead field is confirmed to carry the business line**, so `deal_specifics` is suppressed for every note type and the denominator is 80, not 100. Suppressed is a *state*, not a zero — the weight leaves the denominator. | Set `business_line_field` and `deal_specifics_applicable` in `units/structured_intelligence/config.py`; nothing else moves. Past judgements are **not** recomputed — they carry `config_version` so a reader can see which rubric produced them. | `ASSUMPTION[Q13]` |

`tests/test_assumption_markers.py` fails if any of these markers stops appearing in `src/`, this README, and `ASSUMPTIONS.md` together — so a marker cannot be deleted from the code while the documentation still claims it is there.

### The route contract

`POST /api/v1/notes/judgements` and `POST /api/v1/notes/judgements/resubmission` (the router in `api/routes/judgements.py` carries the `/api/v1` prefix; `GET /api/v1/meta/versions` returns the four version strings for this deployment and tenant). Body is **two integers** and nothing else (`{"lead_id": int, "note_id": int}`, `extra="forbid"`): the note is already saved in the CRM and is fetched by id, so **note text is never accepted in a request body on this route**.

**The direct route** — `POST /api/v1/notes/judgements/direct` and `POST /api/v1/notes/judgements/direct/resubmission` — is the **one exception** (`DECISION[DIRECT_ROUTE]`, ASSUMPTIONS §3.7), added because the CRM's read surface has been unavailable for six weeks. Its body is `{"lead_id": int, "note_id": int, "author_id": int, "note_text": str, "lead": {"leadType", "enquiryType", "project", "status"}}`, `extra="forbid"` on both the body and `lead`, and no field of any score-like shape. The CRM's guarantee is what makes it safe to accept: the text is **the note as stored**, sent **server-side after the save** (so a scoring outage still cannot block a save), with **the note author's own JWT forwarded** — the same gates, the same per-user cost cap and the same rate limit keyed on `sub` — and **`author_id` is the CRM's stored author**, trusted as that, never checked against `sub`, and never a reason to reject a request. Two length limits apply: a hard ceiling of **4,000 characters** on `note_text` in the schema (**422**, before the pipeline is entered) and a soft limit of **2,000 characters** (`TenantConfig.max_note_chars`) checked with the thin gate on the stripped text, which returns **200 suppressed** `not_scorable` / `note_too_long` with nothing reserved and nothing spent — that one applies to **both routes**. Both numbers are provisional; the real sample's longest note is 340 characters. Everything after the length check is the same code on both routes. **The fetch route above remains the contract**; the direct route exists while the read surface is down.

A `200` carries one of two shapes, never a mixture:

- **Judged** — `analysis` (note type, vagueness, missing components, the clarification question, reasoning), `score` (total, band, denominator, and all five components in fixed order with `mark: null, suppressed: true` on the suppressed ones), `decision` (action, whether a prompt was sent, `prompt_withheld`, attempt counts), `suppressed: null`, `versions`, `request_id`.
- **Suppressed** — `score: null`, `decision: null`, `suppressed: {reason, detail_code}`, and `analysis` reduced to the classifier's answer if one was made. Either the note was too thin to judge (before any model call, costing nothing) or it was not scorable (`system_event`, `unclassifiable`).

Both carry `versions`: `rubric_version`, `prompt_version`, `model_version` as the answering call reported it, and `config_version`. **The band and the total are derived in code and never accepted from any input** — a `band` or `total` field in a model answer is malformed output.

`/resubmission` returns the same shape with `decision.prompt_sent` always `false` and `prompt_withheld: "resubmission"`; the attempt counter is read, never incremented; `decision.original_note_fingerprint` carries the first-prompted fingerprint or `null`. An edited note is a new fingerprint and therefore a new judgement, not a `409`.

`prompt_withheld` is `null` both when a prompt was sent and when the decision was `accept_silent` — there was nothing to withhold. It is set only when a prompt that *would* otherwise have been sent was not, and then to one of `resubmission`, `attempt_cap`, `rate_limited`, `nothing_to_ask`.

**Hitting the clarification rate limit is not a `429`.** It returns `200` with `prompt_withheld: "rate_limited"`: the judgement was still produced and is still worth returning, and a `429` would tell the CRM the request failed when it did not.

**In the request lifecycle, the rate limit costs one round trip, and only when it can change the answer.** It is consulted after the decision's first two conditions. A resubmission or a note at its attempt cap makes no rate-limit call at all. When a prompt would be sent, one Lua script takes the slot only if the subject is still under the limit, so the check and the increment are the same step and two concurrent judgements cannot both take the last slot. When there is no question to ask, one plain read runs, so an exhausted window still reports `rate_limited` ahead of `nothing_to_ask`. The rate limit fails open, like the attempt counter.

**Every judgement runs under one deadline**, `DODEAL_JUDGEMENT_DEADLINE_SECONDS`, measured from the entry point, so it includes the fetch. A judgement that has not finished by then answers **503 `judgement_deadline_exceeded`**, and the call it was waiting on is cancelled, not left running.

Error bodies are a fixed `{detail, reason, request_id}`: `invalid_request` 422 · `note_not_found` 404 · `lead_not_found` 404 · `duplicate_request` 409 · `token_budget_exceeded` 429 · `idempotency_unavailable` 503 · `backend_unavailable` 503 · `model_unavailable` 503 · `malformed_output` 503 · `judgement_deadline_exceeded` 503 · `load_shed` 503 · `payload_too_large` 413.

### The token budget

Two counters on the cost connection, in their own `tokens:*` namespace, sharing no key with Gate 4's per-request `cost:*` counters: `tokens:tenant:{tenant}` and `tokens:user:{tenant}:{sub}`, both on `DODEAL_COST_WINDOW_SECONDS`. "Requests made" and "tokens spent" are different quantities, and neither is allowed to stand in for the other.

**In the request lifecycle, the pre-flight sits between the reservation and the first model call.** The idempotency key is claimed first (it is the only thing between a double-submit and paying twice), then `token_preflight` reads both token keys, and only then is a prompt sent. A tenant or user **at or above** either limit is refused with **429 `token_budget_exceeded`** in the standard `{detail, reason, request_id}` body, and the reservation is released like every other non-200 after reserving, so the caller can retry once their window rolls. At or above, not over: the counters are charged after the fact, so by the time a total reaches the cap the budget is already gone.

**The charge happens once per model response that reports usage**, inside `llm_call.complete_once` — which every paid call passes through exactly once, **including a reprompt's discarded first answer**. So a scored judgement charges three times, a judgement that reprompted one pass charges four, and a suppressed note charges nothing because no model ran. A response reporting no usage charges nothing and logs nothing.

Three events, all on the `dodeal_ai.cost` logger, all structured fields and never a prompt or a completion:

| Event | Level | When |
| --- | --- | --- |
| `tokens_charged` | `INFO` | Once per charged response. Carries `tenant`, `subject`, `request_id`, the `profile` **name**, `input_tokens`, `output_tokens`, and both running totals. Numbers, ids and a profile name — nothing derived from a note. |
| `token_budget_warning` | `WARNING` | The first time a running total crosses `limit × DODEAL_COST_TOKEN_WARNING_RATIO` within its window. Names which key crossed, the ratio, the total and the limit. Once per crossing, decided by comparing the pre-call and post-call totals — no second key and no in-process flag. It is a **warning only**: nothing degrades, and what should happen at the ratio is an open decision. |
| `token_charge_bypassed` | `WARNING` | Redis was unreachable when a charge was attempted. The tokens were spent whether or not we counted them, so this is a hole in the meter, not in the bill. |

A fourth, `token_preflight_bypassed` (`WARNING`), is logged **on every bypass** when the pre-flight cannot reach Redis. Until Piece N.3 it was logged once per process, which also hid a second outage after a recovery. The breaker's own transition lines are the de-duplication now. Any bypass line refused by an open breaker, rather than by a store that failed to answer, carries `breaker: open`. Everything here fails **open**, because a money guard is not a security guard.

Two more events, on the `dodeal_ai.breaker` logger, one per transition and nothing in between:

| Event | Level | When |
| --- | --- | --- |
| `breaker_opened` | `WARNING` | A connection's breaker opened, either after `DODEAL_BREAKER_FAILURE_THRESHOLD` consecutive failures, when its single half-open probe failed, or when that probe ended without an answer. Carries `breaker` (`cost` or `operational`). Calls refused while it stays open log nothing more here. |
| `breaker_closed` | `WARNING` | The half-open probe succeeded and the breaker closed. Carries `breaker`. |
| `breaker_probe_abandoned` | `WARNING` | A half-open probe did not answer. Either it ended without one (cancelled, or failed in our own code), and `breaker_opened` follows because the window is re-armed. Or no answer had come back a full `DODEAL_BREAKER_OPEN_SECONDS` after it was admitted, and the next caller became the probe in its place — a probe task that was never resumed. Or the pool refused the probe with `PoolExhausted` before the store was asked, and the slot went straight back: the state reverts to OPEN on an **already elapsed** window, so this line is **not** followed by `breaker_opened` and the next arriving call probes at once (register item 95). Carries `breaker`. Without it a probe that never answers would leave the breaker refusing every call until restart (register item 80). |

### `X-Idempotency-Key` — not required

The campaign decided **not** to require a caller-supplied idempotency header. **The content is the idempotency:** the key is `idem:{tenant}:judge_note:{note_id}:{fingerprint}`, where the fingerprint is a hex SHA-256 of the fetched note text as UTF-8 with no normalisation. It is reserved after the fetch and before any model call, and it lives in two states (register item 82):

- **While the judgement runs, the reservation is short:** `IDEMPOTENCY_INFLIGHT_MULTIPLIER` (4) × `DODEAL_JUDGEMENT_DEADLINE_SECONDS`, so **100 s** at the default. The CRM's own retry of a request that is still running meets `409`. A worker killed outright runs no cleanup, and its key expires on this TTL instead of locking the note for a day.
- **Once the judgement exists, the key is confirmed** (`SET XX`, value `done`) for the tenant's `idempotency_ttl_seconds`, **24 h**. A suppression by the classifier confirms the same way. A confirm that fails is logged as `idempotency_confirm_bypassed` and leaves the short TTL to run out. The judgement is still returned.
- **On any exit that does not produce a judgement, the reservation is released.** That covers a `model_unavailable`, the deadline, a client disconnect and a worker shutdown alike. The release runs on `BaseException`, because a cancellation is not an `Exception`, and it is shielded, so a second cancellation cannot kill the `DEL` in flight. A retry is then judged, rather than meeting a `409` for work that was never done.

This is the safer default: a caller that forgets the header, or reuses one, cannot cause a duplicate paid judgement or suppress a legitimately different one. An edited note is a different fingerprint and is judged again, which is the behaviour the clarification loop needs. **A header remains open for joint design with the CRM** if they want caller-side retry semantics of their own; it would layer on top of the fingerprint, never replace it. If the operational store is unreachable the route **denies** (`503 idempotency_unavailable`) rather than risking duplicate paid work — the one fail-closed policy among the three state concerns.

## Security model

- **Fail-closed authentication and tenancy.** Any failure in Gate 1 or Gate 2 denies the request. A client only ever sees a generic 401 or 403; the specific reason is written only to the audit log.
- **Fail-open cost enforcement.** A Redis outage does not deny requests through Gate 4; it allows them and logs the bypass loudly, since a bounded, observable, recoverable spend risk is preferable to an outage over a non-critical dependency. `/ready` reflects this policy: a Redis outage returns `200` with a degraded body rather than `503`, so the pod is not pulled out of rotation over a dependency the request path already tolerates. The two connections are reported in two fields (`redis` for db1, `operational` for db2) because their failure policies differ — one flag would let a dead idempotency store read as "Redis ok". Both are probed concurrently, so two dead connections cost one probe's time rather than two. **An orchestrator's readiness `timeoutSeconds` must still exceed `DODEAL_REDIS_CONNECT_TIMEOUT_SECONDS` plus `DODEAL_REDIS_SOCKET_TIMEOUT_SECONDS`** (0.25 + 1.0 = 1.25s by default): below that, a Redis outage times the probe out and kills a pod that is serving correctly — which is the exact outage the fail-open policy exists to prevent.
- **Generic client responses, detailed internal logs.** No exception message, claim value, or stack trace ever reaches a response body. The real reason lives only in the structured audit log or the internal error log.
- **Structured, single-line JSON logging.** Every `dodeal_ai` logger writes one JSON object per line to standard output, ready for a log collector. Deny lines are never dropped or sampled.
- **Server-side prompt assembly.** Caller-supplied data is always treated as data, never as an instruction, and is placed in a clearly delimited section that cannot be escaped by forging the delimiter. This includes the note text the direct route accepts in its body: it is delimited by `build_prompt` exactly as fetched text is, and nothing about being sent rather than fetched changes how it is handled.
- **Note text is never accepted in a request body — on the primary route.** `JudgementRequest` is two integers and `extra="forbid"`, so posting note text there is a 422 that does not quote the text back. The direct route (`DECISION[DIRECT_ROUTE]`) is the one exception and is documented above; a sentinel note posted to it appears in no log line at any level, on the 200, either 422, the 503 or the 500 (`tests/security/test_log_safety.py`).
- **Tenant isolation by subdomain.** The token's tenant claim is authoritative; the request's host subdomain must match it, or the request is denied.
- **OWASP LLM Top 10 checkpoint for Unit A.** [`docs/security/owasp-llm-unit-a.md`](docs/security/owasp-llm-unit-a.md) records the five items that apply to the judgement pipeline (LLM01, 02, 05, 07, 10), the control in code that answers each, and the test that would fail if it were removed — plus why the other five do not apply today.

## Documentation index

| Document | Contents |
| --- | --- |
| `ASSUMPTIONS.md` | The single seam ledger: every provisional decision, organized by status (confirmed, built, parked, pending, deferred), the seam it lives behind, and how to correct it. |
| `docs/architecture.md` | The recorded directory tree and Phase 0 build order. |
| `docs/FUTURE_PATTERNS.md` | Patterns to adopt at specific later phases, referenced by trigger comments in the code. |
| `docs/STATUS.md` | The build register: build position, decisions, the audit-finding register, open questions and their owners, and the steps ahead. |
| `docs/decisions/` | Design notes for decisions taken before the dependent code is written. `0001` covers the principal model (who calls us, and as whom) and the execution model (one, not three). |
| `docs/runbooks/secret-rotation.md` | How to rotate the JWT verification key and the per-tenant `DD-API-KEY`s, why rotation means a deploy, and how to establish blast radius from the audit log after a compromise. |
| `docs/security/owasp-llm-unit-a.md` | The OWASP LLM Top 10 checkpoint for Unit A: which items apply, the control in code that answers each, and the test that proves it. |
| `CONTRIBUTING.md` | Local setup, architectural rules, deliberate decisions, the security checkpoint habit, and commit style. |

## Contributing

Read `CONTRIBUTING.md` before making a change. It covers local setup, the architectural rules that tooling cannot enforce on its own, the decisions that are deliberate and should not be reversed without a separate discussion, and the commit message convention used in this repository.
