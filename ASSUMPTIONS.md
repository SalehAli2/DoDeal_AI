# ASSUMPTIONS

Every item here is UNCONFIRMED and built behind a seam so the real answer changes
ONE place. Listed with how to correct it when the answer lands.

## 1. Token type = JWT, HS256
- **Assumed:** the backend sends a JWT we verify with a symmetric HS256 key.
- **Reality:** UNCONFIRMED. The Hikal README says both "JWT-based" and
  `auth:sanctum` — different mechanisms. May instead be a Sanctum token or a
  signed service context (Q1).
- **Seam:** `core/auth/verify.py` — the `TokenVerifier` protocol. `JwtVerifier`
  is today's implementation.
- **How to correct:** write a new `TokenVerifier` (e.g. `SanctumVerifier`,
  `ServiceContextVerifier`) with the same `verify()`/`settings` interface and
  return it from `get_verifier()` in `core/auth/dependencies.py`. Gates, mapping,
  and context are untouched.

## 2. Algorithm = HS256 (symmetric)
- **Assumed:** HS256, key held by us.
- **Risk:** a symmetric key can MINT tokens, not just verify — if confirmed,
  push for RS256 (verify-only public key) or treat key protection as critical.
- **Seam:** `core/config.py` — `jwt_algorithm` (config-driven).
- **How to correct:** set `DODEAL_JWT_ALGORITHM` (e.g. `RS256`) and supply the
  verifying key via `DODEAL_JWT_SIGNING_KEY`. No code change.

## 3. Claim names = tenant_id / sub / roles
- **Assumed:** internal names map to these wire claim keys.
- **Reality:** UNCONFIRMED — may be org_id / user_id / role (Q4).
- **Seam:** `core/auth/claims.py`, reading `core/config.py`
  (`claim_tenant_id` / `claim_subject` / `claim_roles`). The ONLY place claim
  keys are named.
- **How to correct:** set `DODEAL_CLAIM_TENANT_ID` / `DODEAL_CLAIM_SUBJECT` /
  `DODEAL_CLAIM_ROLES` to the real keys. No code change.

## 4. iss / aud = test placeholders
- **Assumed:** `iss=hikal-test-issuer`, `aud=dodeal-ai-test`.
- **Seam:** `core/config.py` — `jwt_issuer`, `jwt_audience`.
- **How to correct:** set `DODEAL_JWT_ISSUER` / `DODEAL_JWT_AUDIENCE` to the real
  values.

## 5. Role table = agent, viewer only (placeholder)
- **Assumed:** `agent` -> lead:read/note:read/note:write; `viewer` ->
  lead:read/note:read.
- **Reality:** UNCONFIRMED — Product hasn't signed off; ~9 real roles expected
  (Q4). Permissions may also need fetching via `/api/getPermissions` rather than
  a static table.
- **Seam:** `core/authz/permissions.py` — the `_ROLE_PERMISSIONS` dict.
- **How to correct:** replace that one dict with the real roles/grants, or point
  `resolve_permissions` at a config/backend-fetch source. Default-deny means
  unmapped roles stay safe until then.

## 6. Tenant cross-check = optional X-Tenant-ID header
- **Assumed:** tenant arrives (if at all) as an `X-Tenant-ID` header, used only
  to cross-check the authoritative token tenant. Absent header = token stands.
- **Reality:** UNCONFIRMED — tenancy is subdomain-based on their side; the
  indicator that reaches us may be a subdomain/host (Q2).
- **Seam:** `core/tenancy.py::check_tenant` (logic) + `gate2_tenant` in
  `core/auth/dependencies.py` (where the value is read off the request).
- **How to correct:** extract the real indicator (e.g. subdomain) in
  `gate2_tenant` and pass it as the cross-check arg. If a tenant indicator is
  ALWAYS sent, tighten `check_tenant` so absent -> deny. Logic seam unchanged.

## 7. request_id = placeholder until middleware exists
- **Assumed:** `request.state.request_id`, falling back to `"unknown"`.
- **Reality:** the always-on request-id middleware (`middleware/`) isn't built
  yet (not in Phase 0 Part-A scope).
- **Seam:** `build_context` in `core/auth/dependencies.py`.
- **How to correct:** when request-id middleware lands, it sets
  `request.state.request_id`; the fallback stops being used. No gate change.

## 8. Scaffolding to remove before feature work
- `src/dodeal_ai/api/routes/_probe.py` is a TEMPORARY probe route that exists
  only to exercise the gate chain for the exit demo. **Delete it before the
  first real feature route ships.**

## 9. Out of scope today (by instruction, not oversight)
- **Backend tool client / `get_lead`** — blocked on Q4; not built.
- **Security Part B** (component-to-component auth) — stub/interface only per
  spec; no second internal component exists yet.
- **Cost controls / quota (Gate 4, 429)** — architecture lists it in the chain;
  not part of today's Part-A scope.
- **Audit retention/encryption (90+ days)** — deployment/log-collector config,
  not code.

## 10. Cost gate (Gate 4) + Redis — BUILT, atomic, placeholder caps
- **Status:** built and wired into the gate chain (`gate4_cost` in
  `core/auth/dependencies.py`). Per-tenant and per-user counters live in
  Redis and are incremented together in ONE atomic Lua script execution
  (`EVAL`, `core/cost/limiter.py::enforce_cost`) — both counters always move
  together (with expiry set on first creation), or, on a Redis failure,
  neither does.
- **Fail-open, unchanged by the atomicity work:** if Redis is unreachable,
  `enforce_cost` still allows the request and logs `cost_cap_bypassed`
  (WARNING, `dodeal_ai.cost` logger). Money guard, not a security guard —
  Auth and Tenancy stay fail-closed.
- **`amount` hook:** `enforce_cost(tenant, subject, amount=1)` — a future
  caller can pass a real token/dollar cost once one exists; both counters
  increment by `amount` in the same atomic step. Nothing calls the LLM yet,
  so only tests exercise anything but the default.
- **Read-only metering:** `get_usage(tenant, subject) -> (tenant_count,
  user_count)` reads current counts without incrementing (plain `MGET`).
  Unlike `enforce_cost` it does NOT fail open — a Redis error here
  propagates, since a reporting read isn't a request-blocking decision. Not
  wired into any route yet.
- **Still placeholder:** `cost_per_tenant_limit` (10000), `cost_per_user_limit`
  (1000), `cost_window_seconds` (86400s / 24h) in `core/config.py` — unvalidated
  against real usage.
- **How to correct:** set `DODEAL_COST_PER_TENANT_LIMIT` /
  `DODEAL_COST_PER_USER_LIMIT` / `DODEAL_COST_WINDOW_SECONDS`; no code change.
  Wiring `amount` to a real per-call token count is a call-site change in
  whichever unit eventually calls the LLM — `enforce_cost`'s signature
  already supports it.

## 11. Input size limit — DEFERRED to last, home undecided
- **Status:** built once as ASGI middleware, then removed — it caused a
  regression (forced eager config load at construction; tangled middleware
  ordering with the error handler).
- **Decision pending:** where it should live. Options: middleware, or closer to
  the route, or alongside the request-id middleware when that is built (both are
  always-on edge concerns).
- **How to correct:** re-add LAST, on its own, run the full suite immediately,
  and watch the error/chain/audit tests. Read the cap lazily (never call
  get_settings() in middleware __init__). Config field `max_request_body_bytes`
  already exists as a placeholder (1 MB) if it was kept.

## 12. Per-unit prompt-injection hardening — deferred to each unit
- **Built now:** the injection-resistant prompt BUILDER (server-side assembly,
  trusted/untrusted split, delimiter neutralisation) and its structural tests.
  This is generic and reused by every unit.
- **Deferred:** each unit's REAL versioned prompt and its task-specific
  adversarial injection tests. These depend on what the unit does and cannot be
  finished until the unit exists.
- **How to correct:** when a unit is built, add (a) its real prompt file in
  prompts/ and (b) adversarial injection tests against that prompt. The builder
  and its boundary do not change.
- **Caveat:** the delimiter boundary is defense-in-depth, not a perfect
  guarantee — no scheme makes an LLM fully injection-proof. It establishes the
  structural boundary and removes easy escapes; hardening is layered and evolves.

## 13. Placeholder config values added this session
- **Watchdog** (`core/config.py`): `external_call_timeout_seconds = 10.0`,
  `external_call_retry_once = True`. Placeholders — tune to real LLM/tool
  latency later.
- **Redis client timeouts** (`core/redis.py`): `socket_connect_timeout=2.0`,
  `socket_timeout=2.0` on both named clients, added so a hung Redis can't
  block a request indefinitely. Placeholder — unlike the other values in this
  list, NOT config-driven today (hardcoded); promote to `Settings` fields if
  they need tuning without a code change.
- **Input size** (if the field was kept): `max_request_body_bytes = 1_000_000`
  (1 MB). Placeholder — tune to real payload sizes.
- **How to correct:** set the matching `DODEAL_*` env vars; no code change.

## 14. Watchdog retry vs non-idempotent writes
- **Assumed:** retry-once is safe for READS.
- **Risk:** for a non-idempotent WRITE (the future note-writeback, a POST) a
  blind retry could double-execute (e.g. create two notes).
- **Seam:** `core/resilience.py::call_with_watchdog` accepts `retry=False`.
- **How to correct:** when wrapping the note-writeback, pass `retry=False` or
  make the operation idempotent. Do not retry writes blindly.

## 15. Sample schema + sample prompt are placeholders
- **`schemas/lead_v1.py`** (LeadV1: id/name/status, extra="forbid") is a SAMPLE
  to build/test the validator. Real lead shape is confirmed when the backend
  tool client is built (blocked). Replace as `lead_v2` or edit the file.
- **`prompts/unit_a_v1.txt`** is a SAMPLE to build/test the prompt builder. Real
  Unit A prompt replaces it when the unit is built.
- The validator and the builder themselves are production-ready; only the
  sample contract/template are provisional.

## 16. Global error handler is ASGI middleware (not an exception handler)
- **Why:** Starlette 1.3.1's ServerErrorMiddleware re-raises after handling,
  which under the test client bypasses an installed 500 handler. The catch-all
  is implemented as outermost middleware so behaviour matches in tests and prod.
- **Note for future middleware:** middleware is applied in reverse registration
  order (last registered = outermost). Register the error catch-all last.
- **request_id in error responses** is currently `"unknown"` until the
  request-id middleware exists (same placeholder as the gates).