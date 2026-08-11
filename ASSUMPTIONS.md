# ASSUMPTIONS

The single seam ledger for the service. Every provisional decision, its status,
the seam it lives behind, and how to correct it when the real answer lands.

Status meanings:
- CONFIRMED — verified with the backend (Waqas).
- PENDING — awaiting a backend or product answer.
- PARKED — built and tested, but not wired into the live path, awaiting a decision.
- BUILT — built, wired, and working (may still hold placeholder values).
- DEFERRED — deliberately not built yet.

---

## CONFIRMED (verified with backend, Waqas)

### 1. Token type — Tymon JWT, verified offline
- The inbound token is a Tymon JWT, verified offline against a shared secret.
  It is not a Sanctum token.
- Seam: `core/auth/verify.py` (the `TokenVerifier` protocol; `JwtVerifier` is
  today's implementation).
- How to correct (if it ever changes): write a new `TokenVerifier` with the same
  interface and return it from `get_verifier()` in `core/auth/dependencies.py`.
  Gates, claim mapping, and context are untouched.

### 2. Algorithm — HS256 today, RS256 requested and agreed
- HS256 (symmetric shared secret) is in use now. RS256 has been requested and
  agreed with the backend, but the public key is not yet provisioned. On the
  local test key until the real key is provided.
- Note: with HS256 the held secret can MINT tokens, not only verify them. RS256
  removes that risk (the private key stays with the backend). This is why RS256
  was requested.
- Seam: `core/config.py` `jwt_algorithm` (config-driven).
- How to correct: set `DODEAL_JWT_ALGORITHM=RS256` and point the key config at
  the public key. No code change.
- Unrelated to the lead/notes integration (item 7/8): RS256 provisioning is a
  Gate 1 (our own JWT verification) concern only. The lead/note endpoints use
  DD-API-KEY, not a JWT, so RS256's status does not block or affect that
  integration. Track them separately.

### 3. Claim names — sub / subdomain / database
- The token carries `sub` (user id), `subdomain` (tenant), and `database`, plus
  standard iat/exp (and nbf/jti/prv). `sub` is an INTEGER (e.g. 42), normalised
  to a string internally. The token carries NO role or permission claims.
- Seam: `core/auth/claims.py` reads names from `core/config.py`
  (`claim_subject`, `claim_subdomain`, `claim_database`).
- How to correct: set `DODEAL_CLAIM_SUBJECT` / `DODEAL_CLAIM_SUBDOMAIN` /
  `DODEAL_CLAIM_DATABASE`. A rename is a config change, not a code change.

### 4. No iss / no aud
- The token carries neither `iss` nor `aud`. Those checks are removed. `exp` is
  present and verified.

### 5. Integer sub handling
- PyJWT rejects a token whose `sub` is not a string. The Tymon `sub` is an
  integer, so PyJWT's `sub` check is disabled (`verify_sub: False`) and the
  claim layer validates and normalises `sub` instead.
- DO NOT re-enable PyJWT's `sub` verification; it would reject valid tokens.

### 6. Tenant — subdomain, authoritative, Host must match
- The tenant is the `subdomain` claim, and it is authoritative. The request also
  arrives at a tenant host (`<subdomain>.dodealcrm.com`); the Host subdomain must
  match the token subdomain, or the request is denied (403).
- Isolation is enforced backend-side by tenant database, keyed on subdomain.
  Lead records carry no tenant field, so the re-check is a subdomain match, not a
  field comparison.
- An absent or subdomain-less Host is a hard 403 (deliberate). Consequence:
  `localhost` fails Gate 2; local testing needs a tenant Host header
  (e.g. `nasir3.dodealcrm.com`).
- Seam: `core/tenancy.py` (logic) and `gate2_tenant` in
  `core/auth/dependencies.py` (reads the Host header).

### 7. Lead + note integration — CONFIRMED (was: posts.data wrapper, guesses)
- Lead list, single lead, and notes endpoints are CONFIRMED (backend
  integration guide). Base `https://<tenant>.dodealcrm.com/api/service`:
  `GET /leads` (paginated, newest first), `GET /leads/{id}` (single lead, 404
  if missing, 422 if id not numeric), `GET /leads/{id}/notes` (that lead's
  notes, newest first; empty `data: []` with a 200 is valid, not an error).
- Lead-list and notes-list response shape is `{status, data, meta}`. `data`
  holds the array directly. The earlier `posts.data` wrapper assumption (and
  the `{success, message, data, meta}` one before that) were both wrong.
- Confirmed lead fields: id (required), name, phone, email, leadType,
  enquiryType, project, status, source, feedback, priority, language,
  leadFor, country, assignedToManager, assignedToSales, bookedAmount,
  createdAt, updatedAt. Every field except id may be null.
  - `bookedAmount`'s exact type is still unconfirmed (nullable; modelled as
    an optional float).
  - `phone` comes back masked for the service credential (deliberate,
    confirmed backend behaviour, not a bug in this service).
  - `createdAt`/`updatedAt` are confirmed ISO-8601 with a timezone offset;
    kept as `str`, not parsed to `datetime`, since nothing downstream needs
    them parsed yet.
  - `extra="ignore"` on every model: unlisted fields the backend may add are
    tolerated, not rejected (external response we do not control; the
    backend has said its data is frequently incomplete).
- Single-lead response envelope (`GET /leads/{id}`) is UNCONFIRMED. The guide
  documents the list and notes shapes but not this one. Modelled by analogy
  as `{status, data}` (a single lead, no `meta`) in
  `schemas.lead.LeadResponse`. Revisit if a real response contradicts this.
- Notes response shape is CONFIRMED: `{status, data, meta}`, each note
  `{id, note, author, author_id, createdAt}`. Only `author` is nullable
  (null if the original author's account was deleted).
- Seam: `schemas/lead.py` (contracts), `tools/leads.py` (`LeadsClient`).

### 8. Service-to-service auth — DD-API-KEY only, no JWT
- Data fetches (leads, single lead, notes) use a per-tenant `DD-API-KEY`
  header against `https://<subdomain>.dodealcrm.com/api/service/...`,
  subdomain taken from the request's authoritative context. These endpoints
  take NO JWT — DD-API-KEY is the only credential, confirmed.
- No role filtering at the API level: the DD-API-KEY service credential
  returns the WHOLE tenant's lead/note data; the backend does not scope
  results by role or by assigned user. See item 12 (Gate 3) — this does not
  resolve the role model, but confirms any role-based filtering has to
  happen application-side, not by relying on the backend.
- Seam: `tools/leads.py`; key and base domain in `core/config.py` (placeholder
  key until per-tenant provisioning).

---

## BUILT (built, wired, working — may hold placeholder values)

### 9. Cost gate (Gate 4) + Redis — atomic, metered, fail-open
- Built and wired into the gate chain (`gate4_cost` in
  `core/auth/dependencies.py`). Per-tenant and per-user counters live in Redis
  and increment together in ONE atomic Lua script (`EVAL`,
  `core/cost/limiter.py::enforce_cost`) — both move together (expiry set on first
  creation), or on a Redis failure neither does.
- Fail-open: if Redis is unreachable, `enforce_cost` allows the request and logs
  `cost_cap_bypassed` (WARNING). Money guard, not a security guard — auth and
  tenancy stay fail-closed.
- `amount` hook: `enforce_cost(tenant, subject, amount=1)` — a future caller
  passes a real token count once one exists; both counters increment by `amount`
  in the same atomic step. Nothing calls the LLM yet.
- Read-only metering: `get_usage(tenant, subject)` reads current counts without
  incrementing (`MGET`). Unlike `enforce_cost` it does NOT fail open — a Redis
  error propagates, since a reporting read is not a request-blocking decision.
- Placeholder caps: `cost_per_tenant_limit` (10000), `cost_per_user_limit`
  (1000), `cost_window_seconds` (86400) in `core/config.py`. Correct via the
  matching `DODEAL_*` env var; no code change.

### 10. Request-id middleware + structured logging
- Request-id middleware is built (`middleware/request_id.py`): reads an inbound
  `X-Request-ID` or generates one, sets `request.state.request_id`, echoes it on
  the response, and carries a `RequestObservability` object with the fuller id
  set (trace_id today; prompt_version/model_version/workflow_version reserved).
  So `request_id` is now a real value everywhere, not "unknown".
- Structured logging is configured (`core/logging_config.py`, called from the
  lifespan): one JSON line per record to stdout, `dodeal_ai` tree at
  `DODEAL_LOG_LEVEL` (default INFO) so audit `allow` lines are not dropped.
  `cost_cap_bypassed` flows through this same pipeline.

### 11. Global error handler is ASGI-level (not an exception handler)
- The fail-closed catch-all runs inside Starlette's own outermost
  ServerErrorMiddleware, before the gate chain. Deliberate 401/403/429 pass
  through untouched; anything else is logged internally at ERROR with the
  request_id and returned as a generic 500.
- Why: Starlette's ServerErrorMiddleware re-raises after handling, which under
  the test client bypasses an installed 500 handler. Placing the catch-all at
  the ASGI boundary makes behaviour match in tests and prod.
- Note for future middleware: middleware is applied in reverse registration
  order (last registered = outermost). Middleware ordering has caused two
  regressions — add new middleware alone and run the full suite immediately.

---

## PARKED (built and tested, not wired; awaiting a decision)

### 12. Gate 3 — permission enforcement
- The token carries no roles, so the permission model is undecided. CONFIRMED
  (item 8): DD-API-KEY-fetched leads/notes are NOT role-filtered by the
  backend — a service credential always returns the whole tenant. This
  narrows what Gate 3 needs to decide (any per-role scoping is ours to build,
  not inherited from the backend) but does not resolve the role table itself.
- The live chain ends at auth + tenancy (`build_context`).
  `resolve_permissions` / `require_permission` remain in
  `core/authz/permissions.py`, unit-tested in isolation, ready to wire when the
  model is confirmed. Identity and RequestContext keep an empty `roles` field so
  shapes are stable.
- Role table today is a placeholder (`agent`, `viewer`) in the
  `_ROLE_PERMISSIONS` dict; ~9 real roles expected. Permissions may need
  fetching via a backend endpoint rather than a static table. Default-deny means
  unmapped roles stay safe until then.
- How to correct: replace the dict with the real roles/grants (or point
  `resolve_permissions` at a config/backend source) AND wire the gate into the
  chain — do both together so a real user is not under/over-granted.

---

## PENDING (awaiting a backend or product answer)

- RS256 public-key provisioning (item 2). Unrelated to the lead/notes
  integration (item 7/8), which uses DD-API-KEY, not a JWT.
- Permission model: real role names and grants (item 12). Role-FILTERING
  behaviour is now confirmed: none, at the API level (item 8).
- Per-tenant DD-API-KEY provisioning (item 8).
- Real end-to-end fetch — PENDING deploy and per-tenant key provisioning, not
  blocked. The client (`tools/leads.py`), schema (`schemas/lead.py`), and
  tests are complete; only a live key against a real tenant is needed to
  exercise it end to end. This is the one item blocking formal Phase 0
  closure (exit-demo criterion 3).
- Single-lead (`get_lead`) response ENVELOPE is unconfirmed (item 7) — the
  method itself is built and tested against an assumed `{status, data}`
  shape.
- `dd_api_key` has a non-fail-closed default (`test-dd-api-key`), unlike the
  signing key. Give it the same fail-closed treatment once it is load-bearing
  (inert today; `tools/leads.py` is not wired to a route).

---

## DEFERRED (deliberately not built yet)

### Input size limit — home undecided
- Built once as ASGI middleware, then removed after a regression (forced eager
  config load; middleware-ordering conflict with the error handler).
- Re-add LAST, on its own, run the full suite immediately, and watch the
  error/chain/audit tests. Read the cap LAZILY — never call `get_settings()` in
  middleware `__init__`. A `max_request_body_bytes` placeholder (1 MB) may exist
  in config.

### Per-unit prompt-injection hardening
- BUILT now: the injection-resistant prompt BUILDER (server-side assembly,
  trusted/untrusted split, delimiter neutralisation) and its structural tests.
  Generic, reused by every unit.
- DEFERRED: each unit's REAL versioned prompt and its task-specific adversarial
  injection tests — they depend on what the unit does. When a unit is built, add
  its prompt in `prompts/` and adversarial tests against it. The builder and its
  boundary do not change.
- Caveat: the delimiter boundary is defense-in-depth, not a perfect guarantee —
  no scheme makes an LLM fully injection-proof.

### /ready vs fail-open consistency
- FIXED: `/ready` returns 200 with a degraded body when Redis is down (matching
  the cost gate's fail-open policy), 503 only when required config is missing,
  and checks only the cost Redis (not the unused queue connection). Kept here as
  the record of the decision.

### Lead-list query parameters — not implemented
- The confirmed spec documents `page`, `per_page` (default 25, max 100 -> 422
  over), `since` (ISO-8601, filters on `updatedAt`), `feedback`, `leadStatus`,
  `leadSource` as query parameters on `GET /leads` (item 7/8). None of these
  are wired into `LeadsClient.get_leads()` today — it takes no parameters and
  always fetches the default page.
- Deliberately out of scope for the initial integration (not requested); add
  as keyword arguments to `get_leads()`, forwarded as query params, when a
  caller needs pagination or filtering.

### Per-endpoint error handling for get_lead / get_lead_notes
- The confirmed spec documents specific status codes for `GET /leads/{id}`
  (404 if missing, 422 if id not numeric). `LeadsClient` does not surface
  these distinctly today — any non-2xx response fails through the watchdog
  into a generic `ExternalCallError`, the same as every other transport
  failure, matching the existing pattern used by `get_leads()`.
- If a caller needs to distinguish "lead not found" from a generic failure,
  add that handling deliberately in `tools/leads.py`, not by weakening the
  watchdog's fail-closed default.

---

## Deliberate decisions — DO NOT reverse

- Gate 3 parked (item 12).
- PyJWT `verify_sub` disabled — integer sub (item 5).
- Cost gate enforcement fails OPEN; reporting (`get_usage`) fails LOUD (item 9).
- Auth and tenancy fail CLOSED.
- The error handler catches broad `Exception` on purpose (item 11).
- `.env` holds only a throwaway LOCAL signing key; it is gitignored. The real key
  comes from the backend (shared secret if HS256, or a public key if RS256),
  injected via a secret manager. Never commit real secrets. The `.env` file must
  be UTF-8 with NO BOM (a BOM corrupts the variable name on Windows).
- `LeadNote` fields are modelled stricter than `Lead` fields: only `author` is
  nullable; `note`, `author_id`, `createdAt` are required. The spec's "treat
  all as optional except id" instruction was scoped to leads only and was not
  repeated for notes (item 7) — do not loosen notes to match leads' blanket
  optionality without a confirmed reason.

---

## Watchdog retry vs non-idempotent writes
- retry-once is safe for READS. A non-idempotent WRITE (a future note-writeback)
  could double-execute on retry.
- Seam: `core/resilience.py::call_with_watchdog` accepts `retry=False`.
- How to correct: when wrapping the note-writeback, pass `retry=False` or make
  the operation idempotent (see FUTURE_PATTERNS item 1, idempotency keys). Do not
  retry writes blindly.

---

## Placeholder config values
- Watchdog (`core/config.py`): `external_call_timeout_seconds = 10.0`,
  `external_call_retry_once = True`.
- Redis client timeouts (`core/redis.py`): `socket_connect_timeout=2.0`,
  `socket_timeout=2.0` on both clients — NOT config-driven today (hardcoded);
  promote to `Settings` if they need tuning without a code change.
- Backend client: `dd_api_key = "test-dd-api-key"`, `backend_base_domain =
  "dodealcrm.com"`.
- Correct any config-driven value via the matching `DODEAL_*` env var; no code
  change.

---

## Scaffolding to remove before feature work
- `src/dodeal_ai/api/routes/_probe.py` is a temporary route that exercises the
  gate chain for the exit demo. Delete it before the first real feature route.
- `study.py` is personal Pydantic notes, not part of the application. Remove it
  from the repo.
