# DODEAL AI — Architecture (recorded structure)

Source of truth for the repo layout. Every Claude Code session should read this
before scaffolding, so the tree is not rebuilt from an older version.

Grounded in: dodeal_ai_foundation_report.md (Phase 0 spec) and
dodeal_ai_master_document.md (scope + principles).

---

## The one principle

Build the spine first. `core/` is governance + auth + tenancy + tools — the
foundation every feature reuses. No feature logic ships until the Phase 0 exit
demo passes. The service is READ-ONLY and reaches CRM data only through backend
tools, never a database (separate DB per tenant, switched backend-side).

---

## Tree

    dodeal-ai/
    ├── pyproject.toml
    ├── uv.lock
    ├── .env.example              # documents required config, never real secrets
    ├── .gitignore
    ├── Dockerfile
    ├── docker-compose.yml        # api + redis for local dev (worker: not yet)
    ├── README.md
    │
    ├── .github/workflows/ci.yml  # lint + test + wheel-install check on push
    ├── docs/                     # this file + design decisions
    │
    ├── src/dodeal_ai/
    │   ├── main.py               # FastAPI app + /health
    │   │
    │   ├── core/                 # ── THE FOUNDATION ── everything imports this
    │   │   ├── auth/             # Gate 1: verify JWT / service creds
    │   │   ├── authz/            # Gate 3: permission + scope + role table
    │   │   ├── audit/            # structured JSON deny/allow logger
    │   │   ├── llm/              # provider abstraction, deterministic mode
    │   │   ├── cost/             # enforced per-tenant/user quota (§7)
    │   │   ├── context.py        # RequestContext (frozen)
    │   │   ├── tenancy.py        # Gate 2: tenant-isolation guards
    │   │   ├── resilience.py     # watchdog timeout + retry-once (§6)
    │   │   ├── validation.py     # output-vs-schema enforcement (§6)
    │   │   ├── prompting.py      # server-side prompt assembly (§6)
    │   │   ├── redis.py          # two named connections: queue + cost
    │   │   └── errors.py         # deny paths -> response codes
    │   │
    │   ├── middleware/           # always-on: request-id, logging, timing
    │   ├── prompts/              # versioned LLM prompts (unit_a, unit_b, assistant, sales_automation)
    │   ├── schemas/              # output contracts per feature — ships inside the package (F1)
    │   ├── tools/                # CRM API wrappers — AI's ONLY path to CRM (read-only)
    │   │
    │   ├── units/                # feature logic — EMPTY until exit demo passes
    │   │   ├── structured_intelligence/   # Unit A (fast/sync)
    │   │   ├── call_intelligence/         # Unit B (slow/async)
    │   │   ├── assistant/                 # Unit C1
    │   │   └── sales_automation/          # Unit C2 (deferred, post-pilot)
    │   │
    │   ├── workers/              # Celery: transcription (B), lead engagement (C2)
    │   │   ├── celery_app.py
    │   │   └── tasks/
    │   │
    │   └── api/routes/           # one router per unit
    │
    └── tests/
        ├── unit/
        ├── integration/
        └── security/             # tenant-isolation tests — Phase 0 exit demo

---

## The request pipeline (how auth runs)

Two distinct mechanisms, do not conflate them:

- **middleware/** — always-on, same for every request: request-id, logging,
  timing. Runs on all routes automatically.
- **core/auth via Depends()** — the gates, attached per-route, run as a CHAIN
  of small dependencies so each can be tested in isolation:

  1. verify token   (core/auth)      -> 401 on failure
  2. check tenant   (core/tenancy)   -> 403 on failure
  3. check role     (core/authz)     -> 403 on failure
  4. enforce quota  (core/cost)      -> 429 on breach
  -> builds/reads immutable RequestContext, the single source of tenant identity

Small dependencies (not one fat function) because the exit demo must prove each
gate independently (cross-tenant blocked, bad token rejected).

---

## Shared policies (defined once, imported everywhere)

- **resilience.py** — every external call (LLM + tools) wrapped with timeout +
  retry-once-then-fail-closed. Imported by core/llm and tools/.
- **validation.py** — every tool/LLM output validated against dodeal_ai/schemas
  before use. Called by tools/ and units/.
- **prompting.py** — prompts assembled server-side from dodeal_ai/prompts
  (packaged; DODEAL_PROMPTS_DIR overrides for local iteration only); caller
  input is data, never instructions.

---

## Deliberately NOT built yet

- **Part B (internal component-to-component auth)** — stub/interface only per
  spec. No folder until a second internal component (Unit C1) exists.
- **units/** — all empty. No feature logic before the exit demo.
- **Session-state Redis instance** — Phase 2 (Unit C1), not now. Only queue +
  cost connections are live in Phase 0.

---

## Infrastructure split (who owns what)

- **DevOps owns:** the Redis servers (two isolated instances: queue + session),
  secrets manager, TLS, deploy pipeline/runners.
- **We own:** the client code that connects (core/redis.py), key naming, TTLs,
  what we store, and the FastAPI service itself.

---

## Build order (Phase 0)

1. Skeleton + /health in Docker (done via existing scaffold)
2. core/redis.py (cost + queue both need it)
3. core/resilience.py (tools + llm wrap with it)
4. core/validation.py + core/prompting.py
5. The gate chain (auth -> tenancy -> authz -> cost) via Depends()
6. tools/get_lead — one read-only tool, tenant-scoped, end to end
7. core/audit + core/cost enforcement
8. Exit demo: cross-tenant blocked+logged · bad token rejected · one tool
   returns only tenant A's data
