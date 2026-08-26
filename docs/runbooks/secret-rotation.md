# Runbook — secret rotation

Written from the code as it stands. Every claim below is checkable against a
file named beside it; if the code changes, change this file in the same commit.

**Scope:** the secrets this service holds. It does **not** cover the CRM's own
secrets, TLS material, or the Redis instances (DevOps owns those — see
`docs/architecture.md`, "Infrastructure split").

---

## 1. What secrets exist

| Secret | Env var | Type in `Settings` | Issued by |
| --- | --- | --- | --- |
| JWT verification key | `DODEAL_JWT_SIGNING_KEY` | `str`, **required, no default** | The CRM |
| Per-tenant backend keys | `DODEAL_DD_API_KEYS` | `dict[str, SecretStr]`, default `{}` | The CRM backend, one per tenant |
| LLM provider API key | *(not a field yet)* | `SecretStr`, no default — **lands at step 14** | The model provider |

### `DODEAL_JWT_SIGNING_KEY`

Today the service runs **HS256**, so this is a **symmetric shared secret**
(`core/config.py::Settings.jwt_algorithm`, default `"HS256"`).

> **The important property:** under HS256, a key that verifies a token can also
> **mint** one. This service therefore holds a credential capable of forging any
> user's identity in any tenant. That is not hypothetical — it is exactly why
> RS256 was requested (ASSUMPTIONS §1.2). Treat this key with the same care as
> the CRM's own signing key, because it *is* the CRM's own signing key.

When the backend provisions RS256, this same field holds the CRM's **public
key** (PEM) instead, and the minting risk disappears — a public key verifies and
cannot sign. The field name is kept for env stability; it is the *verification*
key, whatever its shape (`JwtVerifier.verify()` passes it straight to
`jwt.decode`). The switch is configuration only: `DODEAL_JWT_ALGORITHM=RS256`
plus the new key value. No code change — `pyjwt[crypto]` is installed and the
RS256 round-trip is tested in `tests/security/test_verify.py`.

### `DODEAL_DD_API_KEYS`

A JSON map of tenant subdomain to that tenant's key, parsed into
`dict[str, SecretStr]`. The backend's contract is **one key per tenant, valid
only against its own tenant host** (`tools/keys.py` module docstring), so there
is no shared key and no fallback.

Where it is read: **exactly one place in `src/`** — `LeadsClient._headers()`
calls `.get_secret_value()` to build the `DD-API-KEY` header. Nowhere else,
including every failure path, does key material appear. An unknown tenant raises
`BackendKeyError` (reason code `backend_key_missing`) **before any network
call**, carrying the tenant name only.

An empty map does **not** stop startup: the lifespan in `main.py` logs
`backend_keys_missing count=0` at `ERROR` and continues, because the gate chain
and `/ready` must work before any key is provisioned (step 0).

### LLM provider API key

Not yet a field. It arrives with the adapter at step 14 as a `SecretStr` with
**no default and no placeholder**, deliberately the same fail-closed shape as
`dd_api_keys` (`core/config.py`, comment at the end of the LLM block). Until
then `get_llm_client()` raises `LLMConfigurationError("llm_not_configured")`
unless both `DODEAL_LLM_PROVIDER` and `DODEAL_LLM_MODEL` are set.

---

## 2. How each is loaded — and why rotation means deploy

All three load the same way: `pydantic-settings` reads the `DODEAL_`-prefixed
environment (or `.env`) into a `Settings` object, built once by
`get_settings()`.

Two facts decide everything else in this runbook:

1. `get_settings()` is decorated `@lru_cache` (`core/config.py`). It builds
   `Settings` on first call and returns **that same object** for the rest of the
   process's life.
2. `Settings` is `frozen=True` (its `model_config`). The object cannot be
   mutated after construction, by design — configuration is authoritative.

**There is no hot reload. There is no rotation endpoint. There is no signal
handler that re-reads the environment.** Changing an environment variable inside
a running container changes nothing at all.

> **Rotation = deploy.** To rotate any secret: update the value in the secret
> store, then roll the pods. The new value is picked up when a fresh process
> calls `get_settings()` for the first time. Nothing else works, and nothing
> else should be attempted during an incident.

(`get_settings.cache_clear()` exists and the test suite calls it between cases.
It is **not** an operational tool: in a multi-worker deployment it would clear
one worker's cache and leave the others on the old value — a split brain worse
than the thing being fixed.)

---

## 3. Rotating `DODEAL_JWT_SIGNING_KEY` (HS256)

A **coordinated** rotation with the CRM. This service never mints tokens, only
verifies them, so the CRM must change at the same time.

**The constraint:** `JwtVerifier.verify()` passes a **single** key to
`jwt.decode`. There is no key list, no `kid` lookup, no JWKS fetch. So **no
overlap window is possible.** At the instant the key changes, every token minted
under the old key fails with `invalid_token`, and its holder gets a generic 401.

**Procedure**

1. Agree a change window with the CRM team. Prefer low traffic.
2. Backend generates the new secret and stages it in both secret stores.
3. Both sides switch as close to simultaneously as the two deploys allow.
4. Roll our pods. Confirm with a real token minted *after* the switch.
5. Watch the audit log for `gate="auth"` with `reason_code="invalid_token"`. A
   burst that decays as old tokens expire is the expected shape. A burst that
   does **not** decay means the CRM is still minting with the old key.

**The window.** Tokens minted before the switch are rejected until their holders
obtain new ones. Whether that is seconds or a full token lifetime depends on
whether the CRM re-mints on the next request or waits for natural expiry — ask
them which **before** the window, not during it.

**Future ask, not a plan.** RS256 with a **JWKS endpoint** would remove this
problem entirely: the verifier would fetch the CRM's current public keys by
`kid`, the CRM could publish a new key *before* switching to it, and rotation
would need no coordination and cause no 401 window. That is worth asking for
**after** the RS256 public key lands (ASSUMPTIONS §1.2; open question Q3, asked
three times). It is recorded here as a thing to want. **It is not scheduled, not
designed, and not attached to any step.**

---

## 4. Rotating a tenant's `DD-API-KEY`

Per tenant, and **one tenant at a time**. The map is keyed by subdomain, so
changing one tenant's entry cannot affect another's.

**The constraint:** the old key stops working on the backend's side the moment
they issue the new one. We do not control that instant.

**Procedure**

1. Ask the backend to issue a new key **for that one tenant**.
2. Ask one question first: *can both keys be valid at once, even briefly?*
   - **If yes** — deploy ours first with the new key, then have them revoke the
     old one. Zero window.
   - **If no** — there is a window between their switch and our deploy in which
     that tenant's data calls fail. Agree it, keep it short, do it at low
     traffic.
3. Update that tenant's entry in `DODEAL_DD_API_KEYS` in the secret store.
4. Roll the pods.
5. Verify with `uv run python scripts/real_fetch_check.py <tenant-subdomain>
   --live` — one real call. The tenant comes only from the CLI argument or
   `DODEAL_CHECK_TENANT`; the script has no default tenant anywhere.
6. Confirm no `backend_key_missing` for that tenant in the logs.

**A wrong key and a missing key look different.** A wrong key is rejected by the
backend as 401/403, which surfaces as `ExternalCallError` after the call. A
missing entry is caught earlier and never reaches the network:
`backend_key_missing`, logged at `ERROR` by the `dodeal_ai.tools` logger. The
first is their rejection; the second is our configuration. Do not confuse them.

---

## 5. Compromise response

**First: rotate.** Use §3 or §4 above. Do not investigate first — rotation is
cheap and the investigation is not.

**Then: establish blast radius from the audit log.** Every line is one JSON
object on stdout (`core/logging_config.py`). Search terms, by the question you
are asking:

| Question | Search |
| --- | --- |
| Was a forged or stale token presented? | `logger="dodeal_ai.audit"`, `decision="deny"`, `gate="auth"` — then split by `reason_code`: `invalid_token` (bad signature or malformed — **the one that matters for a leaked HS256 secret**), `token_expired`, `token_not_yet_valid`, `invalid_iat`, `bad_algorithm`, `missing_required_claim` |
| Did anyone reach across tenants? | `gate="tenancy"`, `decision="deny"` — `tenant_mismatch`, `invalid_host`, `invalid_tenant_claim` |
| Which tenants were reached, and by whom? | `decision="allow"`, `gate="auth"` — read the `tenant` field, correlate by `request_id` |
| Was a backend key used against a tenant with no entry? | `backend_key_missing` (logger `dodeal_ai.tools`, `ERROR`) |
| Did the spend cap stop enforcing? | `cost_cap_bypassed` (logger `dodeal_ai.cost`, `WARNING`) — the cost gate fails **open** on a Redis outage, so a bypass window is a window with no quota |

Correlate anything you find by `request_id`: the middleware sets one per request
and every audit line for that request carries it. Note that an inbound
`X-Request-ID` is honoured only if it matches `[A-Za-z0-9._-]{1,128}`; anything
else is replaced by a generated uuid4 and the rejected value is never logged
(`middleware/request_id.py`).

**The one piece of good news: there is no data at rest in this service.** It is
read-only and holds no database (ASSUMPTIONS §3.1 — judgements are returned to
the caller, which persists what it chooses; `docs/architecture.md`, "The one
principle"). Redis holds only counters — request and cost tallies keyed by
tenant and subject, with a TTL. So a compromise of *this* service exposes:

- the secrets in its environment (which is what you are rotating), and
- whatever lead and note data was in flight during the window.

It does not expose a store of historical judgements, because there is not one.
The CRM's data is a separate blast radius, reachable only with a valid
per-tenant `DD-API-KEY`, and assessing it is the backend team's call.

---

## 6. Who does what

| Role | Person | Responsibility |
| --- | --- | --- |
| Executes the rotation | Lead engineer | Updates the secret store, rolls the deploy, verifies, watches the logs |
| Issues `DD-API-KEY`s | Backend (Waqas) | Generates and revokes per-tenant keys; answers whether two keys can be valid at once |
| Issues the JWT key | Backend (Waqas) | Generates the HS256 secret; owns the RS256 public key when provisioned (Q3) |
| Supplies the tenant list | Us (Saleh) | Which tenants need keys (open question Q11) |

**Record every rotation** — date, which secret, which tenants, who ran it — in
**ASSUMPTIONS.md §13 (Placeholder config values)**, beside the entry for the
secret that moved. §13 tracks the live configuration state; a rotation that is
not written there is a rotation nobody can reconstruct later.
