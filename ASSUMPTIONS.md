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