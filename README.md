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

- **Middleware** (`src/dodeal_ai/middleware/`) runs on every request regardless of route: request-id generation and load shedding today, with logging and timing reserved for later.
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
| `.gitignore` | Excludes the virtual environment, caches, coverage artifacts, editor files, any `.env*` file except `.env.example`, and the per-developer Claude Code settings (`.claude/settings.local.json`, `.claude/*.local.*`) while keeping the shared `.claude/settings.json` tracked. |
| `.env` | Local environment variables. Not committed. Must be UTF-8 with no byte order mark. |
| `.env.example` | Documents every `DODEAL_*` setting in `core/config.py::Settings` with its default, or `change-me-local-only` for a secret. Audited field-by-field against `Settings`, in the same order. Copy to `.env` and fill in real local values; never holds real secrets. UTF-8, no BOM, LF. |
| `.dockerignore` | Keeps the build context small and secrets out of it: the virtual environment, `.git`, `.env*`, tests, docs, build artifacts, caches, and `study.py`. |
| `Dockerfile` | Multi-stage build. Installs the locked dependencies and the project **non-editable** into a venv, then copies only that venv into a slim runtime image — no source tree in the final image, so this is the same installed-wheel shape `scripts/verify_wheel.py` checks in CI. |
| `.pre-commit-config.yaml` | Local git hooks: ruff lint, ruff format check, and mypy, all run through `uv run` so they use the exact versions locked in `uv.lock`. |
| `docker-compose.yml` | `api` (built from the `Dockerfile`) plus a local Redis 7 container for the cost and queue gates. |
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
| `check_coverage_floors.py` | Enforces per-**file** coverage minimums for the modules on a deny path (`core/auth/**`, `core/tenancy.py`, `core/cost/**`, `core/errors.py`, `core/validation.py`, `core/log_safety.py`). Reads `coverage.json` written by the pytest run. The repo-wide 92% gate is an average and can be paid for by well-covered code elsewhere; these cannot. Fails closed when a pattern matches no file, so a rename cannot silently drop a floor. stdlib only. |
| `verify_wheel.py` | Installs a built wheel into a throwaway venv and, from a temp directory outside the repo, imports every module under `dodeal_ai` and confirms the packaged prompts directory exists and holds `unit_a_v1.txt`. Runs in CI after the wheel is built; proves an installed copy actually works, not just the editable dev install. |

### `src/dodeal_ai/` (top level)

| File | Purpose |
| --- | --- |
| `main.py` | The FastAPI application instance. Defines the startup lifespan (fail-closed config check, then logging configuration, and the two Redis connection pools released on shutdown), registers the request-id middleware and the error handlers, and exposes `/health` and `/ready`. `/ready` returns `503` only when configuration is missing; a Redis outage is `200` with `redis` and `operational` reporting `ok` or `degraded` per connection. |
| `api/routes/_probe.py` | A temporary route that exercises the full gate chain over HTTP. Used for the Phase 0 exit demo. Scheduled for removal before the first real feature route ships. |

### `src/dodeal_ai/core/`

| File | Purpose |
| --- | --- |
| `config.py` | `Settings`, the single source of runtime configuration, built on `pydantic-settings`. Every module that needs a claim name, a JWT parameter, or a Redis URL reads it from here. The signing key has no default, so a missing key raises a fail-closed `ConfigError` rather than letting the service start unable to verify tokens. |
| `context.py` | `RequestContext`, a frozen dataclass built once the gates have run. It is the single, immutable source of tenant, subject, roles, permissions, and request identity for everything downstream. |
| `tenancy.py` | Gate 2. Requires the whole `Host` header to equal `<tenant>.<inbound_base_domain>` — compared case-insensitively, with a `:port` and one trailing dot tolerated and IPv6 literals rejected — and raises `TenantMismatchError` with `invalid_host` (wrong shape or domain) or `tenant_mismatch` (valid shape, different tenant). Checking the whole host, not just its first label, is what stops `<tenant>.evil.com`. `X-Forwarded-Host` is deliberately not read; the seam is marked in the module docstring. |
| `resilience.py` | The shared watchdog for every external call. Wraps an operation with a timeout and, by default, a single retry on failure. Carries an explicit note that writes must not be retried blindly; a caller wrapping a write should pass `retry=False` or apply an idempotency key. |
| `validation.py` | Validates any tool or LLM output against a Pydantic schema before it is used or returned. Rejects and fails closed on any mismatch, and never logs the raw invalid content. The pydantic `ValidationError` is deliberately dropped rather than chained: it carries the rejected value, so anything that formatted the resulting traceback would print the note text or model output that failed. `OutputValidationError` keeps only the label and one (dotted location, pydantic error type) pair per problem. |
| `prompting.py` | Assembles prompts server-side from versioned files in `src/dodeal_ai/prompts/`, resolved lazily (package default, or `DODEAL_PROMPTS_DIR` override) so importing this module never requires settings to be loaded. Caller-supplied data is always placed in a clearly delimited section and neutralized against delimiter injection, so untrusted input can never be mistaken for an instruction. The stable system template is placed first and variable caller data last, which is also the shape prompt caching needs once real LLM calls exist. `build_prompt` returns an `AssembledPrompt` (stable template / delimited variable data / reserved tail) whose `.text` is the flat prompt; the split lets a provider adapter place a cache breakpoint without parsing the prompt. |
| `redis.py` | Exposes two named, lazily created Redis clients — cost and quota counters (db1) and per-request operational state (db2) — each with its own **bounded** `BlockingConnectionPool` built from the four `DODEAL_REDIS_*` budget settings. There is no numeric literal in the module and a test fails the build if one appears, so every timeout and pool bound has exactly one source of truth. Also exposes `check_cost_redis_ready` and `check_operational_redis_ready`, the two probes `/ready` reports. |
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
| `limiter.py` | Gate 4. Enforces per-tenant and per-user request quotas in Redis. Both counters increment together inside a single atomic Lua script, so a Redis failure can never leave one counter updated and the other not. Fails open and logs a warning if Redis is unreachable, since this is a spend guard rather than a security boundary. Also exposes a read-only `get_usage` function for future reporting. |

### `src/dodeal_ai/core/llm/`

| File | Purpose |
| --- | --- |
| `__init__.py` | Re-exports and `get_llm_client()`, the FastAPI dependency that builds the model client. Reads settings lazily; raises the fail-closed `LLMConfigurationError` if `DODEAL_LLM_PROVIDER` or `DODEAL_LLM_MODEL` is unset, and `NotImplementedError` until the adapter lands in Step 14. Gateway concerns (routing, fallback, breakers) grow here, behind the factory. |
| `client.py` | The seam every unit calls a model through: `LLMClient` Protocol (one async `complete()`, prompt passed through unchanged, a required `profile` keyword naming the calling task), frozen `LLMResponse` (OTel-aligned token/finish fields, `text` excluded from repr), `FinishReason`, `LLMProviderError` with enumerated non-interpolated reasons. No provider implementation yet. |
| `profiles.py` | The named model profiles: `PROFILE_UNIT_A_CLASSIFY` / `_VAGUE` / `_SCORE`, the `KNOWN_PROFILES` vocabulary a test greps the unit against, and `resolve_profile(settings, name)` -> `ResolvedProfile`. Holds the fallback rule (an unconfigured name resolves to the `llm_provider`/`llm_model` pair at temperature 0, and no pair either is `LLMConfigurationError`) and the ceiling rule (`effective_max_output_tokens` — a profile may lower a task's ceiling, never raise it). The adapter is what calls it. |

### `src/dodeal_ai/middleware/`

| File | Purpose |
| --- | --- |
| `inflight.py` | Load shedding. Counts the requests inside the app and refuses the one that meets `DODEAL_MAX_INFLIGHT` immediately with **503 `load_shed`**, through the same `{detail, reason, request_id}` body every enumerated error uses. Installed **inside** the request-id middleware and before everything else, so a refused caller still gets an id to quote while the refusal costs a counter comparison and nothing more — no token verification, no tenant resolution, no body read. The `WARNING` line carries the id and the count and never a tenant (the gates have not run, so the only tenant available would be the caller's own unverified claim) or a body (it was never read). The slot is released in a `finally`, so a route that raises cannot leak one. `/health` and `/ready` are exempt: an orchestrator that cannot reach them under load kills the pod, which is the outage this prevents arriving by another route. |
| `request_id.py` | Reads an inbound `X-Request-ID` header if it is present **and** well-formed (`[A-Za-z0-9._-]{1,128}`), or generates a new uuid4, and sets it on `request.state.request_id` for every existing consumer to read. The header is caller-controlled and reaches every audit line and the response, so a value that fails the check is discarded exactly as if absent — and never logged, echoed, or reported back. Also stores a `RequestObservability` object for the fuller identifier set (trace id today, prompt version, model version, and workflow version reserved for later) and echoes the id back on the response. Runs inside Starlette's own outermost error-handling middleware but before the gate chain. |

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
| `structured_intelligence/` | Unit A, fast synchronous note scoring. **Built** (campaign phases A–J): `schemas.py`, `config.py` (the TenantConfig seam — the only source of a weight, threshold, cap or TTL), `state.py` (the three db2 concerns), `classify.py`, `vague.py`, `scoring.py`, `decide.py`, `llm_call.py` (the single untrusted-parse boundary), and `pipeline.py` (the order the whole unit runs in). Read-only; judgements are returned to the caller, which persists them. |
| `call_intelligence/` | Unit B, slow asynchronous call analysis. Empty. |
| `assistant/` | Unit C1, the conversational assistant. Empty. |
| `sales_automation/` | Unit C2, deferred until after the pilot. Empty. |

### `src/dodeal_ai/workers/`

| File | Purpose |
| --- | --- |
| `runner.py` | The arq worker entrypoint (Decision 2): `WorkerSettings` with Redis derived from `redis_queue_url` and an empty `functions` list. Importing it opens no connection. Step 14 adds the lanes and the real tasks. |

### `tests/` (repo-wide guards)

Two tests that read the tree itself rather than running it:

| File | Purpose |
| --- | --- |
| `test_assumption_markers.py` | Marker reconciliation: every `ASSUMPTION[Qn]` is load-bearing in `src/`, promised in this README, and carries a correction path in `ASSUMPTIONS.md`; `SEAM[STEP3]` is the same shape with `docs/STATUS.md` in place of the ledger. |
| `test_no_sync_clients.py` | Greps every module under `src/dodeal_ai/` for a blocking call in the event loop — `import requests`, `requests.`, `httpx.Client(`, `redis.Redis(`, `redis.StrictRedis(`, `time.sleep(`, `urllib.request` — and fails naming the file and line. This service is one loop: a blocking call does not slow the request that made it, it stops every request in the process, `/health` included. The async spelling of each already exists, so a hit is a habit rather than a necessity. |

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
| `test_redis.py` | The named Redis client accessors: each reads its own URL, each builds its own bounded `BlockingConnectionPool` sized from `Settings` (asserted on the pool's own attributes, with no live Redis), both factories stay separate and cached, both readiness probes report their own connection independently, and — the one that keeps the settings honest — the module is parsed and the build fails if any numeric literal has crept back into it. |
| `test_cost_lua.py` | The cost gate's Lua script **actually executed**, against `fakeredis[lua]` in-process (audit M5: the script was previously never run by the suite). Both counters move in one `EVAL`; the returned counts are the stored values; the window is set when a counter is created and is **not** refreshed by later increments; a pre-existing key with no TTL never gains one (audit M4, pinned as it behaves today); and at the cap `enforce_cost` denies — after the counter has already moved, because the script increments before Python compares. The script is imported from `limiter.py`, never retyped. |
| `test_resilience.py` | The watchdog: success on the first attempt, success on retry, failing closed after retry, honoring `retry=False`, and respecting the timeout. |
| `test_lead_schema.py` | The lead and note response schemas: parsing the `data`-wrapped shape, the single-lead and notes envelopes, and failing closed on a malformed one. |
| `test_validation.py` | `validate_output`'s failure path: `OutputValidationError` carries only (dotted location, pydantic error type) pairs, the pydantic error is not chained, and no attribute holds the rejected input. |
| `test_keys.py` | `SettingsKeyResolver`: a known tenant resolves to its configured secret, an unknown tenant raises `BackendKeyError` with no key material (from that tenant or any other) in the exception or the log line, and `SettingsKeyResolver` satisfies the `TenantKeyResolver` Protocol. |
| `test_leads_client.py` | `LeadsClient`: URL construction under `/api/service/...` from the tenant subdomain, that each tenant's request carries *that tenant's own* key (the F2 isolation test), failing closed with zero transport calls and an unwrapped `BackendKeyError` for an unknown tenant, `get_leads`/`get_lead`/`get_lead_notes` reading `data`, and failing closed on a malformed response. |
| `test_prompting.py` | The prompt builder: server-side assembly and that caller-supplied data can never become an instruction. |
| `test_assembled_prompt.py` | `AssembledPrompt`: `.text` byte-identical to the pre-structure output, untrusted text never in `.stable`, tail rendered after the data, `.variable` excluded from repr. |
| `test_llm_seam.py` | The LLM seam: frozen `LLMResponse` with repr-safe text, enumerated `LLMProviderError` messages, `DODEAL_LLM_*` settings and fail-closed provider validation, the factory's configuration errors, the runtime-checkable Protocol, the prompt-type pass-through contract, and that `profile` is a required keyword on a Protocol that still has exactly one method. |
| `test_model_profiles.py` | Model profiles: a configured profile resolving to its own provider, model and temperature; an unconfigured name falling back to the single-model pair at temperature 0; no profile and no pair raising `LLMConfigurationError` with the fixed `llm_not_configured`; malformed JSON, an empty model, an unknown provider and an out-of-range temperature each failing at settings construction; the ceiling rule in both directions; and a grep over `src/dodeal_ai/units/` asserting every `profile=` names a `KNOWN_PROFILES` value. |
| `test_logging_config.py` | The structured logging setup: an allow line actually reaching standard output under the real configuration, a deny line at warning level, the cost-bypass warning reaching the same stream, third-party loggers staying quiet, and the configuration being called from the application lifespan. |
| `test_inflight.py` | Load shedding: a request below the cap passes and the counter returns to zero, a route that raises still gives its slot back, the request at the cap gets `503 load_shed` with the id echoed in the body and the header, the `WARNING` line carries the id and the count and no tenant, a refused request's body reaches no log line, `/health` and `/ready` bypass the cap without taking a slot, and — the one that matters — twenty concurrent requests against a stub slow route through an ASGI transport are admitted **exactly** to the cap and refused exactly beyond it, which is what distinguishes a correct counter from one that merely sheds something. |
| `test_health.py` | `/health` and `/ready`: config missing returns 503, Redis down returns 200 with a degraded body, and Redis up returns 200 with an ok body. |
| `test_startup.py` | The application lifespan logs `backend_keys_missing` at `ERROR` when `DODEAL_DD_API_KEYS` is empty, and does not when it holds at least one tenant. Never refuses to start either way — the gate chain and `/ready` must work before a key is provisioned. |

### `tests/integration/`

Excluded from the default test run (see Testing below).

| File | Purpose |
| --- | --- |
| `fake_backend.py` | A small FastAPI app standing in for the real backend: serves the confirmed lead/note shapes at the real paths and enforces `DD-API-KEY`, returning 401 without it. Test fixture code, not production code. |
| `test_leads_e2e.py` | Runs `LeadsClient` through the real `HttpxTransport` (not the mocked transport the hermetic suite uses), wired to `fake_backend.py` via `httpx.ASGITransport` so no real socket, DNS, or TLS is involved. Proves the real HTTP code path end to end: leads parsed, the API key sent, empty notes handled as a valid result, and a malformed response failing closed. |

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
| `DODEAL_INBOUND_BASE_DOMAIN` | `dodealcrm.com` | The base domain requests to this service arrive under. Gate 2 requires `Host == <tenant>.<inbound_base_domain>`. Kept separate from the outbound domain because the host of arrival is an open question with the backend and may become e.g. `ai.dodealcrm.com` independently. |
| `DODEAL_PROMPTS_DIR` | none | Overrides where prompt templates are read from. Unset uses the copies shipped inside the package (`src/dodeal_ai/prompts/`); set only for local prompt iteration without a rebuild. |
| `DODEAL_EXTERNAL_CALL_TIMEOUT_SECONDS` | `10.0` | The timeout applied to every external call by the resilience watchdog. |
| `DODEAL_EXTERNAL_CALL_RETRY_ONCE` | `true` | Whether the watchdog retries once by default. Individual callers can override this per call. |
| `DODEAL_LLM_PROVIDER` | none | Which model adapter `get_llm_client()` builds. Unset means not configured; the factory refuses. |
| `DODEAL_LLM_MODEL` | empty, refused | The exact pinned model id, set per deployment. No drifting default. |
| `DODEAL_LLM_TIMEOUT_SECONDS` | `60.0` | Per-call timeout for a model call, passed into the watchdog with `retry=False`. Deliberately separate from the 10s external-call timeout. |
| `DODEAL_LLM_MAX_OUTPUT_TOKENS` | `1024` | Default output ceiling, sized with headroom for Arabic. |
| `DODEAL_LLM_PROFILES` | `{}` (empty map) | JSON map of profile name to that task's model choice, e.g. `{"unit_a.classify":{"provider":"anthropic","model":"<id>","temperature":0},"unit_a.vague":{"provider":"anthropic","model":"<id>"},"unit_a.score":{"provider":"anthropic","model":"<id>"}}`. A profile may carry `temperature` (0–1) and `max_output_tokens`. **The fallback rule:** a profile name that is not in this map resolves to the `DODEAL_LLM_PROVIDER` / `DODEAL_LLM_MODEL` pair at temperature 0, so a single-model deployment configures that pair and writes no profiles at all. A profile's `max_output_tokens` may only **lower** a task's ceiling, never raise it. Every value is validated when settings are built: a malformed map, an empty `model`, an unknown provider or a temperature outside 0–1 refuses to start. |
| `DODEAL_REDIS_QUEUE_URL` | `redis://localhost:6379/0` | The work-queue Redis connection, read by the arq worker (`workers/runner.py`). |
| `DODEAL_REDIS_COST_URL` | `redis://localhost:6379/1` | The cost and quota Redis connection used by Gate 4. |
| `DODEAL_REDIS_OPERATIONAL_URL` | `redis://localhost:6379/2` | The operational Redis connection used by the feature units for idempotency reservations, clarification rate limits and per-note attempt counters. A separate logical DB from the cost connection because the failure policies differ: losing the cost store fails open, losing the idempotency store fails closed. |
| `DODEAL_REDIS_CONNECT_TIMEOUT_SECONDS` | `0.25` | How long a connection attempt to either Redis may take. Deliberately far shorter than the read timeout: reaching a listening socket on the same network is a sub-millisecond operation, so a slow connect means the host is gone rather than busy. **Provisional** — sized against a same-network Redis, not measured. Must be positive; `0` is refused at startup. |
| `DODEAL_REDIS_SOCKET_TIMEOUT_SECONDS` | `1.0` | How long a single Redis command may take, including the `/ready` `PING`. This is the number that bounds what a Redis outage costs a request: the cost gate fails open after it elapses, so the request continues rather than hanging. **Provisional.** Must be positive. |
| `DODEAL_REDIS_MAX_CONNECTIONS` | `20` | The size of each connection pool — one pool per named connection, not one shared. The pool is **bounded**: an unbounded pool answers a Redis stall by opening more sockets, which turns one slow dependency into file-descriptor exhaustion. **Provisional.** Must be positive. |
| `DODEAL_REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS` | `1.0` | How long a caller waits for a free connection once the pool is at its cap, before the pool refuses. Without it, "bounded" would mean "blocks forever at the cap", which is a worse outage than the one the bound prevents. **Provisional.** Must be positive. |
| `DODEAL_COST_PER_TENANT_LIMIT` | `10000` | The per-tenant request cap per window. |
| `DODEAL_COST_PER_USER_LIMIT` | `1000` | The per-user request cap per window. |
| `DODEAL_COST_WINDOW_SECONDS` | `86400` | The cost counter window, in seconds. |
| `DODEAL_MAX_INFLIGHT` | `32` | How many requests may be inside the app at once. The next one is refused immediately with **503 `load_shed`** rather than queued behind work the event loop cannot get to. Must be positive; `0` is refused at startup. **Provisional** — a placeholder chosen to be obviously a placeholder, not a measurement. The load lane sets the real number, so treat a `load_shed` line today as "this number is wrong" rather than as capacity. |
| `DODEAL_LOG_LEVEL` | `INFO` | The effective level for the `dodeal_ai` logger tree. Third-party libraries are unaffected. |

`scripts/real_fetch_check.py` (a manual tool, not part of the test suite) also reads `DODEAL_CHECK_TENANT`, a plain environment variable rather than a `Settings` field, as an alternative to passing the tenant subdomain as a command-line argument.

## Testing

- Run the full suite with `uv run pytest`. Coverage runs by default and the build fails if total coverage drops below the floor configured in `pyproject.toml`.
- The suite is fully hermetic: no test opens a live Redis connection, makes a network call, or calls an LLM. Redis is mocked or faked in every test; `tests/unit/test_cost.py`'s `FakeRedis` is the reference pattern for a new test that needs Redis behavior, and `fakeredis[lua]` (`tests/unit/test_cost_lua.py`) is the one for a test that needs Redis to actually execute something — Lua included.
- Security-focused tests live under `tests/security/` and exercise the gate chain over HTTP with `TestClient`. Everything else lives under `tests/unit/`.
- `tests/integration/` is excluded from the default run via a registered `integration` marker (`pyproject.toml`), so it stays out of `uv run pytest` and CI. Run it explicitly with `uv run pytest -m integration --no-cov` (`--no-cov`: the coverage gate is sized for the full hermetic suite, not this handful of tests).

- A `redis_real` marker is registered and excluded from the default run the same way, for a test that needs a **genuine** Redis server rather than a fake. Nothing carries it yet; it exists so the first test that needs a live server does not have to change the default run in order to add itself. Run it with `uv run pytest -m redis_real --no-cov`.
- `scripts/real_fetch_check.py` is a separate, manual, non-pytest script for the one real credentialed call to the live backend, used for joint verification with the backend team. It dry-runs against a guaranteed-unreachable fake host by default; the real call requires an explicit `--live` flag. See the script's own docstring for usage.

## Code quality and tooling

- **Linting and formatting**: [ruff](https://docs.astral.sh/ruff/), run as `uv run ruff check .` and `uv run ruff format --check .`.
- **Type checking**: [mypy](https://mypy-lang.org/), scoped to `src/`, configured with the `pydantic.mypy` plugin for accurate model inference. Run as `uv run mypy`.
- **Pre-commit hooks**: defined in `.pre-commit-config.yaml`. All three checks above run on every commit through `uv run`, so they always use the exact versions locked in `uv.lock`. Install the hooks locally with:

  ```bash
  uv run pre-commit install
  ```

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request. It installs uv and a pinned Python 3.12, then runs `uv sync --locked` to catch a lockfile that has drifted from `pyproject.toml`.

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

The repo-wide guards are not separate jobs — they are ordinary pytest tests and so run inside check 4. `tests/test_no_sync_clients.py` fails the build if a blocking client or a `time.sleep` appears anywhere under `src/dodeal_ai/`, and `tests/test_assumption_markers.py` fails it if a provisional-answer marker stops appearing in the code, this README and `ASSUMPTIONS.md` together.

## Unit A — Project 1 (note judgement)

> **No real provider before step 3 lands.** `get_llm_client()` still raises, and the factory has **no test switch** — one was forbidden for the whole build and remains forbidden. `FakeLLM` is injected through `app.dependency_overrides` and is the only model any of this has run against. The `SEAM[STEP3]` marker sits at the token pre-flight point in `units/structured_intelligence/pipeline.py` and in `core/cost/limiter.py`: it is a **no-op stub today**, and step 3 is what replaces it. Nothing in this unit is marked `[V]` — see `ASSUMPTIONS.md` §8.11.

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

Error bodies are a fixed `{detail, reason, request_id}`: `invalid_request` 422 · `note_not_found` 404 · `lead_not_found` 404 · `duplicate_request` 409 · `idempotency_unavailable` 503 · `backend_unavailable` 503 · `model_unavailable` 503 · `malformed_output` 503 · `load_shed` 503.

### `X-Idempotency-Key` — not required

The campaign decided **not** to require a caller-supplied idempotency header. **The content is the idempotency:** the key is `idem:{tenant}:judge_note:{note_id}:{fingerprint}`, where the fingerprint is a hex SHA-256 of the fetched note text as UTF-8 with no normalisation. It is reserved after the fetch and before any model call, and released on every non-200 outcome after reservation — so a `model_unavailable` can be retried without meeting a `409` for work that was never done.

This is the safer default: a caller that forgets the header, or reuses one, cannot cause a duplicate paid judgement or suppress a legitimately different one. An edited note is a different fingerprint and is judged again, which is the behaviour the clarification loop needs. **A header remains open for joint design with the CRM** if they want caller-side retry semantics of their own; it would layer on top of the fingerprint, never replace it. If the operational store is unreachable the route **denies** (`503 idempotency_unavailable`) rather than risking duplicate paid work — the one fail-closed policy among the three state concerns.

## Security model

- **Fail-closed authentication and tenancy.** Any failure in Gate 1 or Gate 2 denies the request. A client only ever sees a generic 401 or 403; the specific reason is written only to the audit log.
- **Fail-open cost enforcement.** A Redis outage does not deny requests through Gate 4; it allows them and logs the bypass loudly, since a bounded, observable, recoverable spend risk is preferable to an outage over a non-critical dependency. `/ready` reflects this policy: a Redis outage returns `200` with a degraded body rather than `503`, so the pod is not pulled out of rotation over a dependency the request path already tolerates. The two connections are reported in two fields (`redis` for db1, `operational` for db2) because their failure policies differ — one flag would let a dead idempotency store read as "Redis ok".
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
