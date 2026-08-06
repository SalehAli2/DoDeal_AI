# ASSUMPTIONS

Status of every provisional decision in the service. Each item is CONFIRMED
(verified with the backend), PENDING (awaiting an answer), PARKED (built but not
wired, awaiting a decision), or DEFERRED (not built yet).

---

## CONFIRMED (verified with backend, Waqas)

### 1. Token type — Tymon JWT, verified offline
- The inbound token is a Tymon JWT, verified offline against a shared secret.
  It is not a Sanctum token.
- Seam: core/auth/verify.py (TokenVerifier protocol; JwtVerifier implementation).

### 2. Algorithm — HS256 today, RS256 requested and agreed
- HS256 (symmetric shared secret) is in use now. RS256 has been requested and
  agreed with the backend, but the public key is not yet provisioned.
- On the test key until the real key (or RS256 public key) is provided.
- Seam: core/config.py `jwt_algorithm`. When RS256 is provisioned, set
  `DODEAL_JWT_ALGORITHM=RS256` and point the key config at the public key. No
  code change.
- Note: with HS256 the held secret can mint tokens, not only verify them. RS256
  removes that risk (private key stays with the backend). This is why RS256 was
  requested.

### 3. Claim names — sub / subdomain / database
- The token carries `sub` (user id), `subdomain` (tenant), and `database`, plus
  standard iat/exp (and nbf/jti/prv in the standard set).
- `sub` is an INTEGER (e.g. 42), normalised to a string internally.
- The token carries no role or permission claims.
- Seam: core/auth/claims.py reads names from core/config.py
  (`claim_subject`, `claim_subdomain`, `claim_database`). A rename is a config
  change, not a code change.

### 4. No iss / no aud
- The token carries neither `iss` nor `aud`. Those checks are removed. `exp` is
  present and verified.

### 5. Integer sub handling
- PyJWT rejects a token whose `sub` is not a string. The Tymon `sub` is an
  integer, so PyJWT's `sub` check is disabled (`verify_sub: False`) and the
  claim layer validates and normalises `sub` instead.
- Do not re-enable PyJWT's `sub` verification; it would reject valid tokens.

### 6. Tenant — subdomain, authoritative, Host must match
- The tenant is the `subdomain` claim, and it is authoritative. The request
  also arrives at a tenant host (`<subdomain>.dodealcrm.com`); the Host
  subdomain must match the token subdomain, or the request is denied (403).
- Isolation is enforced backend-side by tenant database, keyed on subdomain.
  Lead records carry no tenant field, so the re-check is a subdomain match, not
  a field comparison.
- An absent or subdomain-less Host is a hard 403 (deliberate). Consequence:
  `localhost` fails Gate 2; local testing needs a tenant Host header
  (e.g. `nasir3.dodealcrm.com`).
- Seam: core/tenancy.py (logic) and gate2_tenant in core/auth/dependencies.py
  (reads the Host header).

### 7. Lead response shape — posts.data wrapper
- The lead-list response is `{ "status": bool, "posts": { <pagination>,
  "data": [ <lead> ] } }`. Leads are read from `posts.data`.
- The earlier `{success, message, data, meta}` assumption was wrong.
- Confirmed lead fields: id, leadName, leadContact, leadEmail, leadType,
  project, leadStatus, leadSource, feedback, priority, country,
  assignedToManager, assignedToSales, booked_amount, creationDate, lastEdited.
- Modelling choices to verify against a real response:
  - Only `id` is required; other fields are optional (records may have nulls).
  - Dates (`creationDate`, `lastEdited`) are strings; format not yet known, so
    not parsed to datetime.
  - `booked_amount` is typed float; confirm the backend does not send it as a
    string.
  - `extra="ignore"` on the lead model: unlisted fields the backend may add are
    tolerated, not rejected (this is an external response we do not control).
- Meaning of `feedback` is not yet confirmed; modelled as an optional string.
- Seam: schemas/lead.py, validated via core/validation.py.

### 8. Service-to-service auth — DD-API-KEY + tenant URL
- Data fetches use a per-tenant `DD-API-KEY` header against a tenant URL:
  `https://<subdomain>.dodealcrm.com/api/...`, subdomain taken from the
  request's authoritative context.
- Seam: tools/leads.py; key and base domain in core/config.py (placeholder key
  until per-tenant provisioning).

---

## PARKED (built, not wired; awaiting a decision)

### 9. Gate 3 — permission enforcement
- The token carries no roles, and whether DD-API-KEY-fetched leads are
  role-filtered or all-tenant is unknown, so the permission-enforcement approach
  is not yet decided.
- The live chain ends at auth + tenancy (build_context). resolve_permissions /
  require_permission remain in core/authz/permissions.py, unit-tested in
  isolation, ready to wire when the model is confirmed.
- Identity and RequestContext keep an empty `roles` field so shapes are stable.

---

## PENDING (awaiting a backend answer)

- RS256 public-key provisioning (item 2).
- Permission model: role-filtering behaviour of fetched leads, and the real role
  names (item 9).
- Note / `feedback` shape — awaiting a sample; note schema not built.
- Per-tenant DD-API-KEY provisioning (item 8).
- Real end-to-end fetch — blocked until the backend confirms DD-API-KEY opens
  /api/leads. The tool client runs against a mock today.
- Single-lead (`get_lead`) response shape — only the list shape is confirmed;
  single-lead fetch not built.

---

## DEFERRED (not built yet)

### Cost gate (Gate 4) + Redis
- Not built; pending Redis availability. There is currently no enforced
  token/spend cap. This is safe only while nothing calls the LLM. Build the
  Redis client and cost gate before any unit makes real LLM calls.
- Local Redis needs no credentials; the client would be config-driven and
  fail-closed so the production URL swaps in with no code change.

### Input size limit
- Built once as middleware, then removed after it caused a regression (forced
  eager config load; middleware-ordering conflict). To be re-added last, on its
  own, with a deliberate decision on where it lives. Read the cap lazily; never
  call get_settings() in middleware __init__.

### Per-unit prompt-injection hardening
- The injection-resistant prompt builder and its structural boundary tests are
  built (core/prompting.py). Each unit's real prompt and task-specific
  adversarial injection tests are deferred to when that unit is built.

### Request-id middleware
- Not built. request_id is currently "unknown" in audit and error output.
  Build it before a pilot so requests can be traced through logs.

---

## Placeholder config values
- Watchdog (core/config.py): `external_call_timeout_seconds = 10.0`,
  `external_call_retry_once = True`. Tune to real LLM/tool latency.
- Backend client: `dd_api_key = "test-dd-api-key"`, `backend_base_domain =
  "dodealcrm.com"`. Real key pending provisioning.
- Correct any of these via the matching `DODEAL_*` env var; no code change.

---

## Watchdog retry vs non-idempotent writes
- retry-once is safe for reads. A non-idempotent write (a future note-writeback)
  could double-execute on retry. Wrap writes with retry=False or make them
  idempotent. Seam: core/resilience.py `call_with_watchdog`.

---

## Error handling note
- The global fail-closed catch-all is outermost ASGI middleware, not an
  exception handler (Starlette's ServerErrorMiddleware re-raises after handling,
  which bypasses an installed 500 handler under the test client). Middleware is
  applied in reverse registration order; the catch-all is registered last so it
  wraps everything.

---

## Scaffolding to remove before feature work
- src/dodeal_ai/api/routes/_probe.py is a temporary route that exercises the
  gate chain for the exit demo. Remove it before the first real feature route.