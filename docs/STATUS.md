# STATUS — the build register

**One file that says where the DODEAL AI service is, what has been decided, what is open, and who owes what.**
Sits beside `ASSUMPTIONS.md` (the seam ledger: what we believe about the backend and how to correct it) and does
not duplicate it. This file is about the *build*; ASSUMPTIONS is about the *contract*.

Update rule: every commit that changes a row here updates this file in the same commit. If a row's status
and the tree disagree, the tree is right and this file is wrong — fix the file.

**Last updated:** 10 Sep 2026, after Unit A Project 1 phases A–J, Piece K, Piece L, Piece M and Piece N (N.1, N.2) — the judgement pipeline is built end to end against `FakeLLM` and the vendored fake CRM (head of `scaffold/core-governance-homes`, CI green). **Nothing in Unit A has been verified against a real model or a real backend; nothing is marked `[V]`** (ASSUMPTIONS §8.11).

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
| Async migration (D2, commits a–c) — cost path on `redis.asyncio`; gates, probe and `/health` `async def`; Celery deleted, arq skeleton in | DONE | `d37945b` + `87cab6f` + D2 part 3 |
| **Unit A Project 1 — phase A** — unit schemas + the `TenantConfig` seam | DONE | `df4689b` |
| **Phase B** — db2 operational client; idempotency, rate limit, attempt counter | DONE | `42cd11c` |
| **Phase C** — judgement routes, `TenantScope`, `DodealError`, the token pre-flight stub (real since Piece N.2) | DONE | `317619f` |
| **Phase D** — classification against FakeLLM, `system_event` short-circuit | DONE | `8874958` |
| **Phase E** — vague detection: per-type prompts, fixed missing-components vocabulary | DONE | `b432af1` |
| **Phase F** — scoring: marks from the model, arithmetic in code, Q13 suppression | DONE | `2f2dbfb` |
| **Phase G** — reprompt once via `AssembledPrompt.tail`, then 503 | DONE | `103ce02` |
| **Phase H** — decide, the clarification loop, the rate limit | DONE | `ae62103` |
| **Phase I** — prompt hardening, adversarial suite, OWASP checkpoint, eval marker (7 pieces) | DONE | `ba44c5c` · `335abf4` · `86a0b3c` · `d0c9acf` · `f17da28` · `257da13` · `403afa7` |
| **Phase J** — the ledger commit: README provisional answers, ASSUMPTIONS, STATUS, marker reconciliation | DONE | `d93d936` |
| **Piece K** — the direct judgement route: `judge_note_direct`, `DirectJudgementRequest`, `note_too_long`, `max_note_chars`, `config_version` → `tenant-cfg-default-2` | DONE | `d9e0486` |
| **Piece L** — the hardening trio: `gather_or_cancel` (register item 63), load shedding + the sync-client guard (73, 75), elapsed ms and in-flight count on the outcome lines (72) | DONE | `a6eca66` · `85aa3e0` · `fc346bc` |
| **Piece M** — model profiles on the LLM seam: `profile` as a required keyword on `LLMClient.complete`, `core/llm/profiles.py` (the three Unit A names, `KNOWN_PROFILES`, `resolve_profile`, the fallback rule and the ceiling rule), `ModelProfile` + `DODEAL_LLM_PROFILES` validated at settings construction (register item 77) | DONE | `b263c8b` |
| Step 3 — token counters. **N.1 (`dffeb80`)**: Redis timeouts, bounded pools, `/ready` reports db2, the fakeredis lane. **N.2 (`77df41d`)**: `enforce_token_cost`, the real fail-open `token_preflight` (the no-op stub and its grep marker are gone), `TokenBudgetExceeded` 429, the warning ratio. **Still owed:** the H3 breaker, the M4 TTL fix, policy-per-caller for fail-closed workers, and degradation behaviour at the warning ratio (A9). | PARTIAL | `dffeb80` · `77df41d` |
| Step 4 — tool layer: query params, paging, error taxonomy, retry policy, pooled transport, per-item validation. **Now also: read-after-write bounded re-read on the note fetch (candidate — see §2 debts).** | after step 3 | — |
| Steps 5–13 | per ed3 §15 | — |
| Step 0 — test tenant + key + joint call | BLOCKED on backend (Waqas) | — |
| Step 14+ | BLOCKED on step 0 | — |

Suite at head: **998 tests passing, 1 skipped, 7 deselected; 99.42% coverage** (total floor 92, plus 14
per-file floors on the deny-path and judgement modules — `limiter.py` gained a floor of its own at 100 in
N.2, and `pipeline.py` is at 100% against its floor of 95),
ruff/format/mypy clean, wheel installs and imports in a clean venv. The skipped test is `tests/eval/test_quality_eval.py`, which needs a real model (step 18);
the deselected 7 are the `integration` marker.

### Register items closed in Piece L

Four items off the post-campaign register, in three commits. Each is the whole
item, not a first instalment.

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 63 | The dangling sibling on the gather failure path | `core/resilience.py` gains `gather_or_cancel(*coros)`: cancels every unfinished task AND awaits it, re-raises the first exception unchanged, never swallows a caller's `CancelledError`. `pipeline.py` changes at the one gather call site. Reverting that line fails three tests. | `a6eca66` |
| 73 | Load shedding | `middleware/inflight.py`: an in-process counter, `DODEAL_MAX_INFLIGHT` (32, **provisional** — the load lane sets the real number), **503 `load_shed`** through the `DodealError` taxonomy, installed inside the request-id middleware and before everything else, slot released in a `finally`, `/health` and `/ready` exempt. `LoadShed` joins the taxonomy and `core/errors.py` gains `dodeal_error_response` (middleware cannot raise: `ExceptionMiddleware` is built inside the user middleware stack). | `85aa3e0` |
| 75 | Sync-client guard | `tests/test_no_sync_clients.py` greps `src/dodeal_ai/` for `import requests`, `requests.`, `httpx.Client(`, `redis.Redis(`, `redis.StrictRedis(`, `time.sleep(`, `urllib.request` and fails naming the file and line. Runs inside CI check 4, not as a job of its own. | `85aa3e0` |
| 72 | Elapsed time on the outcome lines | `elapsed_ms`, `classify_ms`, `vague_ms`, `score_ms` and `inflight` on **both** `judgement_completed` and `judgement_suppressed`, on **both** routes. `time.monotonic()` only; the clock starts at the entry point, so the fetch route's two backend calls are inside `elapsed_ms`; a pass that did not run is **null**, never zero. | `fc346bc` |

### Register item closed in Piece M

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 77 | Model profiles on the LLM seam | `profile: str` is a required keyword on `LLMClient.complete`; the factory stays parameterless so `dependency_overrides[get_llm_client]` keeps working. `core/llm/profiles.py` holds the three Unit A names, `KNOWN_PROFILES`, and `resolve_profile(settings, name)`. `ModelProfile` (provider, model, temperature 0–1, optional ceiling) and `llm_profiles` read from `DODEAL_LLM_PROFILES` as JSON, all validated at settings construction. **Fallback:** an unconfigured name resolves to the `llm_provider`/`llm_model` pair at temperature 0; neither is `LLMConfigurationError`. **Ceiling:** a profile may lower a task's ceiling (64 / 1024 / 256), never raise it. The three call sites pass their own constant; a grep test holds them to `KNOWN_PROFILES`. Item 76's adapter is what consumes `resolve_profile`. | `b263c8b` |

### Register items advanced in Piece N.1

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 3 (step 3, first sub-commit) | Redis timeouts and bounded pools; `/ready` reports db2; the fakeredis lane | Four `DODEAL_REDIS_*` budget settings, all `gt=0` so a non-positive value is a `ConfigError` at construction. `core/redis.py` builds a bounded `BlockingConnectionPool` per named connection from those four and holds **no numeric literal at all** — a parse-the-module test fails the build if one returns. `/ready` gains `operational` (db2) beside `redis` (db1), same `PING` probe under the same socket timeout, same `200`-with-`degraded`; missing config is still `503`. `fakeredis[lua]` is a dev dependency and `tests/unit/test_cost_lua.py` executes the real Lua script. A `redis_real` marker is registered and excluded from the default run; nothing carries it yet. | `dffeb80` |

### Register items advanced in Piece N.2

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 24 | Token counters and the real pre-flight | `tokens:tenant:{t}` and `tokens:user:{t}:{s}`, moved by a SECOND Lua script that shares no key with the request script. `enforce_token_cost(scope, input_tokens, output_tokens, profile)` charges once per model response that reports usage, from `llm_call.complete_once` — the one point every paid call passes through, so a reprompt's discarded first answer is charged too. It logs `tokens_charged` (INFO) per call and `token_charge_bypassed` (WARNING) on a Redis outage, and it never denies: the call it charges for is already paid. `token_preflight` is a real MGET of both keys, raising `TokenBudgetExceeded` (**429 `token_budget_exceeded`**) at or above either limit and failing open with a once-per-process `token_preflight_bypassed`. The stub and every `SEAM` marker naming it are gone, and `tests/test_assumption_markers.py` now asserts the marker's **absence**. | `77df41d` |
| 61 (design half) | The token-budget warning ratio | `cost_token_warning_ratio` (0.9, `gt=0` `lt=1`) and one `token_budget_warning` WARNING the first time a running total crosses `limit × ratio` within its window — fired once per crossing by comparing the pre-call and post-call totals, with no second key and no in-process flag. **The design half only.** Nothing degrades at the ratio; what should happen there is A9's decision and is deliberately not made here. | `77df41d` |

**Items 24 and 61 could not be located in this repo either** — the same gap N.1 reported for item 25. No tracked file carries a register numbered 24 or 61; both rows above are written from the Piece N.2 brief's description of them rather than from a register entry read in the tree. If the register lives outside the repo it still needs marking by hand.

**The item numbered 25 could not be found.** No tracked file in this repo has a register item 25 — not this file, not `CAMPAIGN_REPORT.md`, not `README.md`. Piece N.1 was asked to mark items 3 and 25 done; item 3 is above (partly — see the step 3 row), and 25 is recorded here as unlocated rather than guessed at. See "For the lead" in `CAMPAIGN_REPORT.md`, Piece N.1.

**Not in this piece:** register item 9 (gather the lead and notes fetches). `gather_or_cancel` is written so item 9 uses it unchanged — its two-argument form is generic and that is the shape item 9 needs.

### Open items carried out of Unit A Project 1

| Item | Who / when |
| --- | --- |
| **`bookedAmount` may be a string.** Fixture lead `1661` carries `"1,250,000"` where `Lead.bookedAmount` is `float \| None`. The loader skips and counts it; the count is pinned at 1. **Confirm against the real export** — if the backend really sends that, `Lead` is wrong today and the real `LeadsClient` will raise on that lead in production. | backend (Waqas); ASSUMPTIONS §4.6 already asks for the `bookedAmount` contract |
| **Two test files still script the vague/score gather positionally:** `tests/unit/test_judgement_pipeline.py` and `tests/unit/test_judgement_routes.py` (both via a local `_happy_path()`). They pass because `asyncio.gather` steps its tasks in argument order and nothing before the fake suspends — a scheduling accident, not a guarantee. `FakeLLM.script_for` (phase I.2) is the fix; converting them is its own commit. | us, unscheduled |
| **Review finding F4 deferred:** one reprompt tail serves both form failures and content failures; a second tail selected by error class would say something more useful. | post-campaign batch |
| **`.env.example` has been modified-unstaged in the working tree throughout the campaign** and was deliberately never touched (campaign §0.8 forbids it). Someone should look at what that change is and either commit or discard it. | whoever made it |
| **The vendored corpus is 127 notes carrying only 57 distinct texts.** Harmless for the structural eval; misleading for any quality number computed over it. | before step 18 |
| **`DECISION[DIRECT_ROUTE]` — accepted 10 September. BUILT in Piece K.** `POST /api/v1/notes/judgements/direct` and its `/resubmission` variant accept the saved note's text in the body — **the one exception to "no note text in a request body"**, granted because the CRM's read surface has been unavailable for six weeks. The fetch route is unchanged and remains the contract. The prohibition is amended in K's own commit; the decision, its three points and its correction paths are `ASSUMPTIONS.md` §3.7. **What stops it becoming the default path:** the fetch route is the documented contract; the direct body is `extra="forbid"` with no score-shaped field, so it can carry a note and never an answer; and both routes run the same `_judge`, so choosing the direct one buys no different behaviour. It exists while the read surface is down. | done, Piece K |
| **`tenant-c.json` is not vendored.** Only `tests/fixtures/fake_crm/tenant-a.json` exists. Any test that needs a second tenant's corpus — cross-tenant isolation over real-shaped data, or a per-tenant config that actually differs — has nothing to read. Fixture tenants are `tenant-a` / `tenant-b` by convention, so a second corpus would be `tenant-b.json`; `tenant-c.json` is named here because that is how it was raised. | us, unscheduled |

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
- ~~Error taxonomy: one `DodealError(reason_code, http_status, gate)` base + one handler — step 4 or step 6.~~
  **DONE — phase C** (`317619f`). `core/errors.py` carries the base and one handler; every client-visible
  reason code in Unit A goes through it with a fixed `{detail, reason, request_id}` body.
- ~~`TenantConfig` seam (per-tenant weights/thresholds/catalogues, version-stamped) — before step 9.~~
  **DONE — phase A** (`df4689b`). `units/structured_intelligence/config.py` is the only import path for a
  weight, threshold, cap or TTL; `get_tenant_config(tenant)` returns the frozen default for every tenant,
  version-stamped `tenant-cfg-default-1`. Per-tenant *variation* is still future work — the seam exists,
  the values are one set.
- **Read-after-write bounded re-read on the note fetch (candidate) — step 4.** The CRM calls us immediately
  after saving a note. If the backend read is served by a replica, the note may not be there yet and the
  caller gets `404 note_not_found` for a note that exists. Not observed — it cannot be, against a fake
  backend — and deliberately not pre-solved: a blind re-read would double the read cost of every genuine
  404. Decide it at step 4 with the paging work, when there is a real backend to measure against.
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
| H3 | Redis outage costs 2.0s per request in the threadpool; `/ready` ping exceeds k8s default probe timeout | **PARTLY DONE** (`dffeb80`) — the four hardcoded `2.0`s are gone: `DODEAL_REDIS_CONNECT_TIMEOUT_SECONDS` `0.25`, `DODEAL_REDIS_SOCKET_TIMEOUT_SECONDS` `1.0`, and a bounded `BlockingConnectionPool` (`DODEAL_REDIS_MAX_CONNECTIONS` `20`, `DODEAL_REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS` `1.0`) on both clients. The `/ready` ping now runs under the 1.0s socket timeout. **The breaker is NOT built** and is still owed. | N.1 (`dffeb80`) + step 3 |
| H4 | Gate 2 case-sensitive on Host; base domain unchecked | DONE | fix 3 |
| H5 | No JWT clock-skew leeway; skew indistinguishable from forgery | DONE | fix 4 |
| H6 | Tenant claim unvalidated and interpolated into the outbound URL | DONE | fix 3 |
| H7 | RS256 impossible without `cryptography` | DONE | fix 4 |
| M1 | New `AsyncClient` per call; hardcoded transport timeout | PLANNED | step 4 |
| M2 | All-or-nothing page validation; `bookedAmount: float` for an unusable field | PLANNED | step 4 |
| M3 | `get_leads()` silently returns page 1 only | PLANNED | step 4 (rename `get_leads_page`) |
| M4 | Lua sets EXPIRE only on create; pre-existing key without TTL never expires | PLANNED — the behaviour is now **pinned by an executing test** (`tests/unit/test_cost_lua.py`), so the fix has to change that test deliberately | step 3 |
| M5 | Lua never executed by the suite | **DONE** (`dffeb80`) — `fakeredis[lua]` is a dev dependency and `tests/unit/test_cost_lua.py` runs the imported script: atomic pair increment, TTL on create and not on refresh, counts matching stored values, and the deny at the cap through `enforce_cost` | N.1 (`dffeb80`) |
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
| Eval set as a `pytest -m eval` marker (structure vs FakeLLM in CI; quality vs real model on manual trigger) | phase I.7 | **DONE (skeleton)** — marker registered in `pyproject.toml`; `tests/eval/test_structural_eval.py` runs all 127 corpus notes and all 27 timeline events through the pipeline in the default suite; `tests/eval/test_quality_eval.py` is a skipped placeholder awaiting a real model (step 18). The quality half is the part still outstanding. |
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
| Q16 | What is the CRM's timeout on its call to us, and does it **retry synchronously** if we are slow? Three model calls plus two backend reads is not a sub-second route, and a synchronous retry against a route that reserves an idempotency key changes what the second attempt sees — it meets a `409 duplicate_request`, correctly, but only if their timeout is longer than our worst case. | Waqas | OPEN | Whether the per-call ceilings and the 10s global external timeout are compatible with their client; whether `409` is a state their UI can render |
| Q17 | **If the CRM does not store and expose judgements, does this service hold score history?** §5.1 assumes they persist what we return. If they do not, every coaching measure over time is impossible — and the alternative is our first data at rest, with the privacy, isolation and residency consequences that carries. | Business + lead | TO RAISE | Step 15; whether Unit A needs a datastore at all |
| Q18 | **Rate-limit window: 3 per hour, or 3 per day?** And does a fixed thin-state prompt (Q19) count against it? `rate_limit_per_hour 3 / rate_limit_window_seconds 3600` is recorded as our default and is what is built; nobody in the business has confirmed the shape. Three per hour is generous for one rep; three per day is a different product. | Business | TO RAISE (default **3 per hour** recorded and built) | The clarification loop's real cadence; `config.py` if it changes |
| Q19 | **Does a thin note earn a fixed clarification prompt with no model call?** Today a note under the thin-evidence floor is suppressed silently: no prompt, no spend, nothing reaches the salesperson. A fixed, non-model prompt ("this note is too short to judge — what happened?") would be free and arguably more useful than silence. Deliberately not built: it puts text in front of a person, which is a product decision. | Business | TO RAISE | Whether `insufficient_evidence` stays a silent state; interacts with Q18 |
| Q20 | **Data residency and a DPA for note text sent to a model provider — one answer per provider.** Note text is customer content and leaves our infrastructure the moment a real provider is wired. Which providers are permitted, in which regions, under what agreement, and with what retention on their side. | **UNOWNED — the lead finds the owner** | OPEN | **HARD GATE: no real note may reach any provider until this is answered.** Blocks step 16 and step 18, not just step 11 |
| Q21 | Managed Redis: does the chosen offering support `SELECT` (we use three logical DBs — queue 0, cost 1, operational 2)? What is the edge/ingress **body-size limit**? And **where are JSON log lines shipped**, with what retention? | DevOps | UNASKED (in the meeting script) | db2 in production; the audit trail being readable at all; log retention is a BRD non-functional requirement with no owner |

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
| 3 | `enforce_token_cost` beside `enforce_cost` (own namespace, own limits, the shared `cost_window_seconds`); fail-open pre-flight read; the key test (token charge leaves request counters untouched and vice versa). **N.1 DONE (`dffeb80`):** H3 timeouts to `Settings` + bounded pools, `/ready` reporting db2, M5 `fakeredis[lua]`. **N.2 DONE (`77df41d`):** `enforce_token_cost` and its second Lua script, the real `token_preflight` (stub and marker retired), `TokenBudgetExceeded` 429, the warning ratio, `test_token_and_request_counters_never_touch`. **Still owed:** the H3 breaker, the M4 TTL fix, policy-per-caller for fail-closed workers | H3 timeouts (DONE — N.1); H3 breaker; M4 TTL fix; M5 `fakeredis[lua]` (DONE — N.1); M7 client type (DONE — D2 part 1); policy-per-caller for fail-closed workers | D2 accepted (`9dca80d`) |
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
