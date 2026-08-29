# STATUS — the build register

**One file that says where the DODEAL AI service is, what has been decided, what is open, and who owes what.**
Sits beside `ASSUMPTIONS.md` (the seam ledger: what we believe about the backend and how to correct it) and does
not duplicate it. This file is about the *build*; ASSUMPTIONS is about the *contract*.

Update rule: every commit that changes a row here updates this file in the same commit. If a row's status
and the tree disagree, the tree is right and this file is wrong — fix the file.

**Last updated:** 29 Aug 2026, after D2 part 2 — gates, probe and health on `async def` (head of `scaffold/core-governance-homes`, CI green).

Status vocabulary: `DONE` (committed, CI green) · `PLANNED` (prompt written, not run) · `NEXT` (the next step
in the sequence) · `BLOCKED <on>` · `PROPOSED` (decision written, not accepted) · `OPEN` (question asked, no
answer) · `UNASKED` (question identified, not yet sent).

---

## 1. Build position

| Item | Status | Commit |
| --- | --- | --- |
| Phase 0 foundation (gates, audit, error boundary, watchdog, validator, prompt builder) | DONE | pre-existing |
| `AssembledPrompt` (stable / variable / tail; `.text` byte-identical) | DONE | `dfcf4f0` + `21b8d0a` + `d8daded` |
| Step 1 — LLM seam (`LLMClient`, `LLMResponse`, `get_llm_client`, `DODEAL_LLM_*`) | DONE | `339de2e` |
| Step 2 — FakeLLM (`tests/helpers/fake_llm.py`), mypy scope → `tests/helpers` | DONE | `86dc768` |
| Audit fix 1 — package layout, wheel check in CI, Dockerfile, compose api, `.env.example` | DONE | `c81cec9` |
| Audit fix 2 — per-tenant DD-API-KEY via `TenantKeyResolver`, no default, fail closed | DONE | `5be966b` |
| Audit fix 3 — tenant as validated DNS label (claim + Host), full-host check, property tests, 429 over HTTP | DONE | `4decb1d` |
| Audit fix 4 — JWT leeway + distinct skew reason codes, `pyjwt[crypto]`, RS256 tests | DONE | `897b325` |
| Audit fix 5 — log safety (`OutputValidationError` unchained, `log_safety.py`, audit via `extra=`, sentinel tests) | DONE | `dc8371f` |
| Hotfix — root `tests/conftest.py` (signing key + settings cache per test; no `.env` dependence) | DONE | `60125dc` |
| Audit fix 6a — code housekeeping (see §4 for contents) | DONE | `e138149` |
| Audit fix 6b — repo/process housekeeping + ledger corrections (see §4, §7) | DONE | `4805dc1` + `1d795d0` |
| Step 3 — token counters (`enforce_token_cost`, pre-flight read, breaker, TTL fix, Lua under fakeredis) | NEXT | — |
| Step 4 — tool layer: query params, paging, error taxonomy, retry policy, pooled transport, per-item validation | after step 3 | — |
| Steps 5–13 | per ed3 §15 | — |
| Step 0 — test tenant + key + joint call | BLOCKED on backend (Waqas) | — |
| Step 14+ | BLOCKED on step 0 | — |

Suite at head: 218 tests, 98.4% coverage (total floor 92, plus per-file floors on the deny-path modules),
ruff/format/mypy clean, wheel installs and imports in a clean venv.

---

## 2. Decisions

### Accepted (see also ASSUMPTIONS §11 "do not reverse")
- Steps 1–2 design: thin one-method `LLMClient` Protocol; gateway concerns grow behind `get_llm_client()`, never
  onto the interface; temperature fixed at 0 in adapters; `llm_timeout_seconds` separate from the 10s external
  timeout; `llm_model` has no real default.
- Prompt structure: `AssembledPrompt` carries the caching/trust boundary; adapters read `.stable`/`.variable`,
  never re-split `.text`.
- Backend keys: per tenant, `SecretStr`, no default, unknown tenant fails closed before any network call.
- Tenant identity: one DNS-label rule at both boundaries; Gate 2 checks `<tenant>.<inbound_base_domain>`
  case-insensitively; `X-Forwarded-Host` deferred until the host-of-arrival question is answered.
- JWT: leeway 30s default (never 0); reason codes `token_expired` / `token_not_yet_valid` / `invalid_iat`
  distinct from `invalid_token`; RS256 is a config switch (code side ready).
- Logging: foreign exceptions log type only; our exceptions carry fixed-vocabulary messages; tracebacks are
  frames-only and unchained; audit fields travel as `extra=`.
- Tests: every test builds its own `Settings` (root conftest); fixtures never use a real tenant name
  (`tenant-a` / `tenant-b`); sentinel tests raise through a helper so the frame text is not the sentinel.
- Process: one change per commit; stopping chain before every push
  (`uv run pytest; if ($?) { uv run ruff check . }; if ($?) { uv run ruff format --check . }; if ($?) { uv run mypy }`);
  one Claude Code session per fix/step; never amend or force-push; no Co-Authored-By.

### Accepted — design note `docs/decisions/0001-principal-model-and-execution-model.md`
| Decision | Position | Status | Carried into |
| --- | --- | --- | --- |
| D1 — who calls us, and as whom | Principal model with three sources (user JWT today; signed service token mirroring `X-Node-System-Token`; signed job payload for workers); tool layer takes a `TenantScope`, not a `RequestContext` | ACCEPTED as design · which source the CRM caller uses still awaits Q1/Q2 (§6) | Step 6 route skeletons; Unit B step 4 |
| D2 — one execution model | All-async: `redis.asyncio` on the request path, `async def` routes, workers on `arq` (an async-native, Redis-backed runner; a Streams consumer we own is the fallback) instead of Celery; `celery_app.py` deleted, not filled | ACCEPTED · engineering-only | Step 3 (cost client type), step 6, Unit B step 4 |

### Smaller design debts (fold into the step that first needs them)
- Error taxonomy: one `DodealError(reason_code, http_status, gate)` base + one handler — step 4 or step 6.
- `TenantConfig` seam (per-tenant weights/thresholds/catalogues, version-stamped) — before step 9.
- Cost accounting event (tenant, subject, unit, operation, tokens in/out, cached, model) alongside the
  enforcement counters — step 3 or step 14.
- `Run` abstraction for aggregates (bound → refuse or execute → result) so background execution later does not
  change route contracts — step 16.
- State at rest: score history either persisted by the CRM (asks 7–8) or by us (first data at rest, privacy and
  isolation implications) — decide before step 15; today's answer is "the CRM's".
- `TokenVerifier` exposes `settings` on the Protocol (leaks a dependency into the interface) — cosmetic.

---

## 3. Audit findings — register

Severity from the 26 Aug audit. "Landed in" is the commit or the step that carries the fix.

| ID | Finding | Status | Landed in |
| --- | --- | --- | --- |
| F1 | Wheel excluded `schemas/` and `prompts/`; installed app could not import the tool layer | DONE | fix 1 |
| F2 | One global DD-API-KEY with placeholder default | DONE | fix 2 |
| H1 | Validation failures leaked field values into the ERROR log via the chained pydantic error | DONE | fix 5 |
| H2 | Watchdog retried 401/403/404/422 and collapsed them into one error | PLANNED | step 4, first sub-commit |
| H3 | Redis outage costs 2.0s per request in the threadpool; `/ready` ping exceeds k8s default probe timeout | PLANNED | step 3, first sub-commit |
| H4 | Gate 2 case-sensitive on Host; base domain unchecked | DONE | fix 3 |
| H5 | No JWT clock-skew leeway; skew indistinguishable from forgery | DONE | fix 4 |
| H6 | Tenant claim unvalidated and interpolated into the outbound URL | DONE | fix 3 |
| H7 | RS256 impossible without `cryptography` | DONE | fix 4 |
| M1 | New `AsyncClient` per call; hardcoded transport timeout | PLANNED | step 4 |
| M2 | All-or-nothing page validation; `bookedAmount: float` for an unusable field | PLANNED | step 4 |
| M3 | `get_leads()` silently returns page 1 only | PLANNED | step 4 (rename `get_leads_page`) |
| M4 | Lua sets EXPIRE only on create; pre-existing key without TTL never expires | PLANNED | step 3 |
| M5 | Lua never executed by the suite | PLANNED (`fakeredis[lua]`) | step 3 |
| M6 | Inbound `X-Request-ID` trusted verbatim | DONE | fix 6a |
| M7 | Sync Redis client will block the event loop from async unit code | DONE | D2 part 1 |
| M8 | No Dockerfile / `.env.example`; compose without api | DONE | fix 1 |
| M9 | Log formatter merged JSON-shaped messages into top-level fields (forgery path) | DONE | fix 5 |
| M10 | ~~Unused~~ `httpx2` dev dependency — **the finding was wrong**: `httpx2` is Starlette TestClient's preferred client (it falls back to `httpx` with a deprecation warning). Dropped in 6a, restored in 6b; kept. Runtime migration of `tools/httpx_transport.py` at step 4 | DONE (corrected) | fix 6b |
| M11 | Two dev-dependency declarations | DONE | fix 6a |
| L1 | CRLF/LF mix (checkout artefact) → `.gitattributes` | DONE (renormalise changed zero blobs; every index blob was already LF) | fix 6a |
| L2 | `.python-version` ignored; CI Python unpinned | DONE | fix 6a |
| L3 | No `[tool.ruff]` config; rule set drifts with releases | DONE — pinned `target-version` and `line-length`; **no `select`**: the set in effect is ruff 0.16's curated 413-rule default, not expressible as selectors, and listing prefixes would have *added* rules | fix 6a |
| L4 | Docstring drift (`verify.py` done in fix 3; `resilience.py`, `permissions.py`, FUTURE_PATTERNS item 1, `architecture.md`) | DONE | fix 6a |
| L5 | `errors.py` logged `%r` of exceptions | DONE | fix 5 |
| L6 | uvicorn logs not JSON | DONE | fix 6a |
| L7 | `study.py` tracked | DONE | fix 6a |
| — | `LLMConfigurationError` defined twice (config.py and llm/client.py) | DONE — one definition, a `ConfigError` subclass in `core/llm/client.py` | fix 6a |
| — | `require_context` (parked Gate 3) untested, hiding in the aggregate coverage | DONE — deny and allow paths tested; `dependencies.py` 83% → 98% | fix 6a |
| — | Per-module settings fixtures redundant after root conftest | DONE — five removed | fix 6a |
| — | Real tenant name in fixtures | DONE — 102 occurrences across 15 test modules, plus the docs and `real_fetch_check.py` | fix 6a |
| — | `httpx` → `httpx2` migration of `tools/httpx_transport.py`, so the test client and the production transport run on one library | PLANNED | step 4 |

Unit B / cross-unit (plan-only, no code yet): worker identity needs a principal type (D1); `lru_cache` Redis
clients are not fork-safe (moot under D2); `enforce_token_cost` policy must be per caller (workers fail closed,
requests fail open); read-only is protected only by the absence of a write path — the salary firewall needs a
deny-list in code when Unit B's delivery contract is designed.

---

## 4. Fix 6 contents

**6a (code housekeeping) — DONE, `e138149`:** single `LLMConfigurationError(ConfigError)`; `X-Request-ID` validated (M6); uvicorn
loggers through the JSON handler (L6); `require_context` deny-path test; per-file coverage floors
(`scripts/check_coverage_floors.py`, in CI) for `core/auth`, `core/tenancy`, `core/cost`, `core/errors`,
`core/validation`, `core/log_safety`; `nasir3` → `tenant-a`/`tenant-b` sweep and no default tenant in
`real_fetch_check.py`; remove redundant per-module fixtures; delete `study.py`; drop `httpx2`; unify dev deps;
pin ruff selectors + line-length + target; commit `.python-version` and pin CI Python; `.gitattributes`;
docstring drift (L4).

**6b (repo/process) — DONE, `4805dc1` + `1d795d0`:** commit `.claude/settings.json` (wildcard permission rules) and gitignore
`.claude/settings.local.json`; Dependabot for `uv` and GitHub Actions; branch-protection instructions in
CONTRIBUTING (no merge on red, required checks = the four + wheel + floors); `docs/runbooks/secret-rotation.md`
(JWT secret / RS256 public key, per-tenant DD-API-KEYS — today rotation = deploy, `Settings` is cached; JWKS is
the future ask); `.env.example` audit against `Settings`; CONTRIBUTING line on sentinel tests and on per-file
floors being the real gate; commit this STATUS file; apply the ledger corrections in §7.

---

## 5. Practices — placement

| Practice | Placement | Status |
| --- | --- | --- |
| Backoff with jitter on the single retry | step 4 retry-policy sub-commit | PLANNED |
| Outbound `User-Agent: dodeal-ai/<version>` | step 4 transport work | PLANNED |
| 429 path over HTTP | fix 3 | DONE |
| Property-based tests on the two parsers | fix 3 | DONE |
| Per-file coverage floors for security modules | fix 6a | DONE |
| Branch protection + Dependabot | fix 6b | DONE (Dependabot in the tree; branch protection is a repository setting — see CONTRIBUTING) |
| Secret-rotation runbook | fix 6b | DONE (`docs/runbooks/secret-rotation.md`) |
| Metrics + tracing (OTel; `LLMResponse` fields already aligned) | step 6, after D2 | at the step |
| Circuit breaker on Redis | step 3, after D2 | at the step |
| Eval set as a `pytest -m eval` marker (structure vs FakeLLM in CI; quality vs real model on manual trigger) | step 13 | at the step |
| Contract tests against the backend on a schedule | step 0 (needs a key) | at the step |
| Graceful shutdown check under a real orchestrator | first deploy | at the step |
| Log retention (90+ days) and encryption — BRD non-functional requirements | business + DevOps | no owner |
| Wheel-install check in CI | fix 1 | DONE |
| Root conftest — no `.env` or test-order dependence | hotfix | DONE |

---

## 6. Open questions — who owes what

Sent to Waqas (one message, 26 Aug) unless marked UNASKED.

| # | Question | Owner | Status | Unblocks |
| --- | --- | --- | --- | --- |
| Q1 | Does the CRM's call to us after a note saves carry the user's `jwt_token`, or the CRM's own credential? | Waqas | OPEN | D1; step 6; whether Gate 1 admits the call at all |
| Q2 | Which host does that call arrive at (`<tenant>.dodealcrm.com` path/subdomain, or proxied with forwarded Host)? | Waqas + infra | OPEN | Gate 2 in production; `X-Forwarded-Host` handling; `inbound_base_domain` |
| Q3 | RS256 public key — date? (asked three times) | Waqas | OPEN | Not holding a minting-capable secret; signed service token under D1 |
| Q4 | Does adding a note bump the parent lead's `updatedAt`? (ask 3) | Backend | OPEN | Incremental sync; §6.2 fan-out option 2 |
| Q5 | Is `since` compared with offset or naive local time? Internal `lastEdited` is naive local. (ask 3b) | Backend | OPEN | Correctness of every `since` sweep; client sends explicit offset, never `Z`, until answered |
| Q6 | Does `/leads/{id}/notes` return timeline events as well as human notes? (§4.5) | Backend | OPEN | Whether Project 1 scores machine text; must be answered before step 7 |
| Q7 | Is JWT `sub` the same id space as note `author_id`? (§4.3) | Backend | OPEN | Rate limit keying; every per-rep output |
| Q8 | When a rep records "not interested", do they set the lead `feedback` label, write a note, or both? | Business | OPEN | Whether there is text to score |
| Q9 | Is there a rate limit on `/api/service/*` (per key/IP/tenant) and what does it return? | Backend | UNASKED | Step 16 concurrency cap and projected-call ceiling; 429 handling |
| Q10 | Test tenant + key for the joint call (step 0) | Waqas | OPEN (chased) | Phase 0 exit criterion 3; every `[V]` tag |
| Q11 | Tenant list for key provisioning | Saleh | OWED BY US | Q10 |
| Q12 | Notes index on the service surface (ask 4, architectural, with the ~1,463-call arithmetic) | Backend | not yet raised formally | Whether tenant-wide analytics fits the synchronous shape |
| Q13 | Two business lines, one rubric (§5.2): which lead field identifies the line; checklist per line | Business (answerable from the sample) | OPEN | Step 9 |
| Q14 | Written salary firewall (§5.6) | Business | OPEN | Before any write path exists |
| Q15 | Evaluation data: 300–500 notes + 50 hand-scored; which tenant (127 vs ~9,000 notes) | Business | OPEN | Step 17, step 26 |

Client-expectation gaps (BRD/milestones vs settled position) to surface in writing, not reconcile in code:
"repeat until clear" vs prompt-once; "final confirmed note saved" and "score tracked per rep" vs no write path
and no history; "three models tested" as a Phase 0 outcome vs step 11 after the key; BRD read-only vs Unit B/C2
writes.

---

## 7. Ledger corrections to apply to ASSUMPTIONS.md (fix 6b)

- §1.3: `database` claim value is `crm_<subdomain>`.
- §1.7: the single-lead envelope shape is `[D]` (modelled by analogy); the 404/422 behaviour is `[T]`. The code
  already says so.
- §2: add the permissions endpoints (`GET /api/role-permissions/{role_id}`, `GET /api/roles/{role_id}/permissions`)
  as "exist, Sanctum-guarded, unreachable"; note `status = NULL` means active; note the user→role mapping is also
  not in the JWT, so Gate 3 at the source needs two things exposed.
- §4: add ask 3b (`since` offset semantics) and Q9 (rate limit).
- §5: add the host-of-arrival question beside 5.1.
- §7: add "two lead shapes in one CRM — the service surface is a hand-built translation over `posts.data`
  (`leadName`, `leadContact`, `booked_amount`, naive local timestamps); fields added to leads do not appear on
  our surface unless someone adds them".
- §8: entries for 8.4 LLM seam, 8.5 AssembledPrompt, 8.6 packaging, 8.7 backend key resolver, 8.8 tenant label
  rule, 8.9 JWT leeway / RS256 readiness, 8.10 log safety.
- §13: `dd_api_key` placeholder paragraph removed (done in fix 2); note Redis socket timeouts still hardcoded
  until step 3.
- §14: `study.py` deletion (fix 6a); README no longer refers to `.env.example` as missing.
- §15 (Unit B review questions): add "which async runtime, and why not Celery" once D2 is accepted.
  DONE — applied in `9dca80d`, with the recorded answer.

---

## 8. Build sequence ahead (steps 3–13; nothing here needs backend access)

| Step | Contents | Carries from the audit / practices | Gate |
| --- | --- | --- | --- |
| 3 | `enforce_token_cost` beside `enforce_cost` (own namespace, own limits, window `None` → `cost_window_seconds`); fail-open pre-flight read; the key test (token charge leaves request counters untouched and vice versa) | H3 breaker + timeouts to `Settings`; M4 TTL fix; M5 `fakeredis[lua]`; M7 client type (DONE — D2 part 1); policy-per-caller for fail-closed workers | D2 accepted (`9dca80d`) |
| 4 | `tools/leads.py`: `page`/`per_page`/`since`/filters as kwargs; paging on `current_page == last_page`; `since` always with explicit offset | H2 typed backend errors + `retry_on`; M1 lifespan-owned `AsyncClient`; M2 per-item validation + `bookedAmount: Any`; M3 `get_leads_page`; backoff with jitter; `User-Agent` | — |
| 5 | `units/structured_intelligence/` schemas (`NoteType`, `NoteAnalysis`, `NoteScore`), version stamps, suppressed-state | `TenantConfig` seam decision | — |
| 6 | Route skeletons behind the gates, dependency override proven | D1 answer (Q1/Q2); metrics/tracing; error taxonomy | Q1 answered |
| 7–9 | Classification, vague detection, scoring (totals in code, bands in code) | Q6 answered before 7; Q13 before 9 | — |
| 10 | Reprompt once, stricter instruction in `AssembledPrompt.tail` | — | — |
| 11 | Clarification loop: third Redis client, key `tenant + lead_id + fingerprint`, TTL, cap 1 | — | — |
| 12 | Rate limit: 3 prompts/hour per verified subject | Q7 | — |
| 13 | Real prompts under `prompts/structured_intelligence/`, adversarial tests, eval marker | — | — |

Step 0 (test key + joint call) runs in parallel and is owned by other people; chase, do not wait.

---

## 9. Process rules in force

- Stopping chain before every push; read every block. `ruff format .` right after writing code.
- One Claude Code session per fix/step; prompts are self-contained (Phase 0 re-inspects the tree).
- Claude Code permissions: wildcard rules in `.claude/settings.json` (committed in 6b); `git push` stays on
  ask; `--amend`, `--force`, `reset --hard` denied; `.env` unreadable.
- Test fixtures: `tenant-a` / `tenant-b`. The real test tenant lives only in the local `.env`.
- CI is the check that matters; local green with a `.env` present is not evidence (root conftest fixed the
  class, but keep the habit).
- Every Claude Code report goes into this file before the next prompt is written.
