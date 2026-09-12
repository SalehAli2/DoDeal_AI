# The fix register

One line per item. The number is the one every prompt, report and STATUS row uses. The reasoning behind an item lives in the master document (section 13) and, for items 1 to 58, in the 3 September review; this file carries what a session needs: the statement, the carrying step, the status and the sha.

Rules:

- A session applies an item only in its carrying step. A prompt that names an item outside its step is wrong; say so in the report.
- A session marks an item DONE here in the backfill commit, with the sha, and nowhere else first.
- A new item is added by the lead from a piece report, never by a session.
- Status words: PENDING (designed, step named), OPEN (an answer is owed by a named party), DECIDED (a decision recorded, not built), NOTE (recorded, no action), RETRACTED, DONE.

Carrying steps, in order (master section 15): N.4b, hand commits (101, `.env.example`), 102, 95, A9a (adapters), A-demo (demo runtime, pre-lane fixes, the load lane 74), A3 (the fix batch), A5 (step 4, the tool layer), A7a, A7b, A6 (step 14), A8 (first real CRM call), A9 (real notes), A10 (metrics), A11 (evaluation), A12 (pilot), A13.

Last updated: 12 September 2026, at `dab744c`, from master ed3r6.

## Items 1 to 13: code, in scope now

| # | Item | Carrying step | Status | Sha |
|---|---|---|---|---|
| 1 | Two-phase idempotency TTL; release only when still in flight | A3, with 2 | DECIDED (D3); the short TTL half landed in N.3b (82) | `8ba3024` (half) |
| 2 | Store the serialised judgement under the key; replay 200 with `Idempotent-Replay: true`; unreadable value is 409; cross-tenant isolation tested | A3 | DECIDED (D3) | |
| 3 | Bounded `BlockingConnectionPool`; Redis timeouts to `Settings`; proven on a real server | Step 3 | DONE (104 and 105 owed as their own items) | `dffeb80`, `44e9071` |
| 4 | One lifespan-owned pooled `httpx.AsyncClient` for the CRM | A5 | PENDING | |
| 5 | `retry_on` predicate (never 4xx) plus jittered backoff | A5 | PENDING, folded into 89 | |
| 6 | Compose and `.env.example` db2 URL | Pre-D | DONE | |
| 7 | Lazy `redis_settings` in `workers/runner.py` | hotfix | DONE | `d0fa1e1` |
| 8 | `cost_cap_bypassed` as `extra=` fields | Pre-D | DONE | |
| 9 | `asyncio.gather` the lead and notes fetches with `gather_or_cancel`; lead failure wins | A3 | PENDING | |
| 10 | `Field(ge=1)` on `lead_id`, `note_id`, `author_id` | A3 first, or with 90 | PENDING | |
| 11 | Non-root container user, `HEALTHCHECK`, `--proxy-headers` | Before pilot, folded into 94 | PENDING | |
| 12 | Cache the verifier per settings instance | A3 | PENDING | |
| 13 | `import-linter` contract plus `CODEOWNERS`; move `current_inflight` to `core/inflight.py` | A3, last | PENDING | |

## Items 14 to 34: code, Unit A, later steps

| # | Item | Carrying step | Status | Sha |
|---|---|---|---|---|
| 14 | Gather vague and score | Phase F | DONE | `2f2dbfb` |
| 15 | Per-task output ceilings as constants, Arabic-sized | Phase G | DONE | `103ce02` |
| 16 | `asyncio.Semaphore` around the provider and the CRM transport | A9 | PENDING | |
| 17 | Bounded read-after-write re-read when page one is empty (Q8) | A5 | PENDING | |
| 18 | Per-item page validation; `bookedAmount: Any`; `rows_rejected` | A5, folded into 90; pulled forward if A8 lands first | PENDING | |
| 19 | Typed backend errors with `Retry-After` | A5, folded into 89 | PENDING | |
| 20 | Circuit breaker on Redis and on the provider | N.3 / A9 | Redis half DONE, hardened N.3b, proven N.4; provider half at A9 with a probe counter | `3199e3d`, `73cf893`, `7a8c48c` |
| 21 | Provider fallback routing | A9 | PENDING | |
| 22 | Minimal metrics surface | A10 | PENDING | |
| 23 | OTel tracing on the `gen_ai.*` fields | A13 | PENDING | |
| 24 | Per-tenant cost accounting event `tokens_charged` | N.2 | DONE | `77df41d` |
| 25 | `/ready` reports db2; pings gathered | N.1, N.2 | DONE | `dffeb80` |
| 26 | `provider_request_id` on the outcome line | A10 | PENDING (the elapsed-ms half is 72) | |
| 27 | Rate-limit decision in one round trip | N.3 | DONE | `3199e3d` |
| 28 | Fingerprint normalisation only if drift is observed | Only if observed | NOTE (R10) | |
| 29 | `pip-audit`; real-Redis lane; `fakeredis[lua]` | N.1 / N.4 / N.4b | `fakeredis[lua]` DONE `dffeb80`; the lane DONE `44e9071`; `pip-audit` at N.4b | `dffeb80`, `44e9071` |
| 30 | Secret-manager `TenantKeyResolver` | Before pilot | PENDING | |
| 31 | Charge the request quota after the fetch instead of at Gate 4 | Not on this branch | OPEN, lead's decision | |
| 32 | `createdAt` parsed once into an aware datetime; guards the direct route's empty `createdAt` | A5 or step 14 | PENDING | |
| 33 | Resubmission carries the original judgement reference | Phase H | DONE | `ae62103` |
| 34 | `language_detected` on every judgement | A3 | PENDING | |

## Items 35 to 79: Unit B design, non-code, retracted, added

| # | Item | Carrying step | Status | Sha |
|---|---|---|---|---|
| 35 to 38 | Unit B: `max_tries=2` then dead-letter; tier from duration before any paid call; uncertainty flag; job key with objection-list and checklist versions | Unit B | PENDING, design held | |
| 39 | Q16 rewritten: only the CRM's timeout value is owed | | OPEN | |
| 40 | Gate 2 behind a rewriting proxy is a pilot checklist line | | NOTE | |
| 41 | Q9 unasked | Meeting script | OPEN | |
| 42 | `noeviction`, AOF, `SELECT` support, pool sizing on the managed Redis | | OPEN, DevOps | |
| 43 | Body-size limit at the edge | | OPEN, DevOps; 87 adds one in the app as well | |
| 44 | Section on the stored judgement as first data at rest | | DONE (master) | |
| 45 | `ASSUMPTION[Q1]` note: the per-user cap degenerates under a service credential | Phase J | DONE | `d93d936` |
| 46 | Threat model: the API pod environment is CRM-root-equivalent under HS256 | | NOTE | |
| 47 | Fixed-window burst; 12-hour tokens without `jti` | | NOTE | |
| 48 | Reprompt-rate measurement; possible `extra="ignore"` on model outputs | A11 | PENDING | |
| 49 | arq 0.25 plus redis 8.1 runtime compatibility | Before step 14 | PENDING | |
| 50 | Unit B job-state model in db2 plus dead-letter | Unit B | PENDING | |
| 51 | STATUS section 6 row: A7 daily summary, three versions in scope (R26) | With the A7a STATUS rows | OPEN, row absent at `dab744c` | |
| 52 | STATUS section 6 row: A-3 and rolling 30-day measures read the CRM's judgement store (R25) | With the A7a STATUS rows | OPEN, row absent at `dab744c` | |
| 53 | ASSUMPTIONS 3.6: B7 access audit, B2 Arabic summary, B13 storage country | housekeeping | DONE | `5863986` |
| 54 | Unit B alerting precondition: item 22 before the first pilot recording | A10, B12 | NOTE | |
| 55 | Fingerprint read-path drift risk | | NOTE, with 28 and R10 | |
| 56 | Reservation before the fetch | | RETRACTED (thin evidence must decide before anything is reserved) | |
| 57 | `EXISTS` plus `SET` | | RETRACTED (`SET NX EX` is one command) | |
| 58 | Attempt cap keyed on fingerprint | | RETRACTED (a resubmission must read the same counter) | |
| 59 | Redaction of phone- and id-shaped tokens before egress, on either route | A9 | PENDING | |
| 60 | The latency number for the CRM, measured on the load lane under the deadline; the combined-prompt alternative priced | A-demo; refined at A11 | PENDING | |
| 61 | Budget degradation at 90 % of the token budget | N.2 / A9 | Warning half DONE; degradation behaviour at A9 | `77df41d` (half) |
| 62 | Q17 to the business | | CLOSED by R25 | |
| 63 | Cancel the sibling when one of `gather(vague, score)` fails; `gather_or_cancel` | L.1 | DONE | `a6eca66` |
| 64 | Thin-state fixed clarification prompt, no model call, `TenantConfig.thin_prompt_enabled` | A3, last, if Q19 is yes | OPEN, business | |
| 65 | Deterministic no-contact fast path | Decide at A9 with the real per-call cost | OPEN, lead | |
| 66 | Rate-limit window as a business decision (Q18) | A3, with 64 | OPEN, business | |
| 67 | Residency / DPA answer is a hard gate on A9 (Q20) | Before A9 | OPEN, unowned | |
| 68 | FakeLLM prompt-directed scripting | I.2 | DONE | `335abf4` |
| 69 | Vendor the fake CRM fixtures | I.1 | DONE for `tenant-a.json`; `tenant-c.json` with 74 | `ba44c5c` |
| 70 | Piece K: the direct judgement route | K | DONE | `d9e0486` |
| 71 | Soft note-length limit in `TenantConfig`, one place for both routes | K | DONE | `d9e0486` |
| 72 | Elapsed milliseconds per request and per pass on the outcome lines; in-flight count | L.3 | DONE | `fc346bc` |
| 73 | Load shedding: `DODEAL_MAX_INFLIGHT`, immediate 503 `load_shed` | L.2 | DONE | `85aa3e0` |
| 74 | Load lane: `tests/load/`, `load` marker, scenarios in master 17.1, `tenant-c.json`, a request counter on the fake CRM | A-demo | DECIDED | |
| 75 | Test that greps `src/` for sync clients, `time.sleep`, `urllib.request` | L.2 | DONE | `85aa3e0` |
| 76 | Provider adapters: `OpenAICompatibleClient` (Groq, OpenAI) and `GeminiClient`; `LLM_API_KEY`; JSON mode; status and finish-reason mapping; per-provider temperature bound; conformance tests on `MockTransport`; `scripts/model_smoke.py` | A9a | DECIDED | |
| 77 | Model profiles: `profile` keyword on `complete`; `core/llm/profiles.py`; `LLM_PROFILES` | M | DONE | `b263c8b` |
| 78 | Demo runtime: `DODEAL_BACKEND_SCHEME`, `.env.demo`, `scripts/mint_demo_token.py`, compose Redis | A-demo | DECIDED | |
| 79 | Fake CRM (other repo): a note-save endpoint that calls us; a request counter; a minimal display; `tenant-c` served | A-demo | DECIDED | |

## Items 80 to 96: the 11 September code review

| # | Item | Carrying step | Status | Sha |
|---|---|---|---|---|
| 80 | Breaker re-arms on a cancelled or failed probe; an abandoned probe is taken over | N.3b | DONE | `73cf893` |
| 81 | `PoolExhausted` excluded from the breaker count; pool sized `max_inflight + 4` | N.3b | DONE | `7a8c48c` |
| 82 | Reservation released on `BaseException` under `asyncio.shield`; short in-flight TTL; confirmed to 24 h once judged | N.3b | DONE | `8ba3024` |
| 83 | One `asyncio.timeout` per judgement, fetch included; 503 `judgement_deadline_exceeded` | N.3b | DONE | `83bc11b` |
| 84 | Build the LLM client once in `lifespan`; fail startup on `ConfigError`; `/ready` fails closed without it | A9a, with 76 | OPEN | |
| 85 | Preload all nine prompt templates at startup; a missing file fails startup | A9a, with 76 | OPEN | |
| 86 | Rewrite `RequestIDMiddleware` and `InflightMiddleware` as pure ASGI | A-demo, before 74 | OPEN | |
| 87 | Pure-ASGI body-size limit, outermost, 64 kB, 413 above it, before Gate 1 | A-demo, before 74 | OPEN | |
| 88 | `JsonFormatter` formats `exc_info` frames only; a stdout-wide sentinel test through the ASGI stack | A-demo, before 74 | OPEN | |
| 89 | `_retryable` predicate (5xx, 429, transport, timeout) plus jitter in the watchdog; folds 5 and 19 | A5 | OPEN | |
| 90 | Per-item page validation with `rows_rejected`; `bookedAmount: Any`; `Field(ge=1)`; folds 10 and 18 | A5; per-item half pulled forward if A8 lands first | OPEN | |
| 91 | `jwt_signing_key` as `SecretStr`, read in `JwtVerifier` only; `raise ConfigError from None` | Before pilot (A12) | OPEN | |
| 92 | Direct route: reject `str(author_id) != sub` unless the caller is a service principal; key `attempt:*` on the subject as well | Before pilot; revisit at Q1 | OPEN | |
| 93 | `/_probe/protected` out of the production app | Before pilot | OPEN | |
| 94 | Deployment: non-root, `HEALTHCHECK`, graceful shutdown, `--proxy-headers`, `terminationGracePeriodSeconds`; folds 11 | Before pilot | OPEN | |
| 95 | A half-open probe refused by the pool reverts to OPEN without refreshing `_opened_at`, so the next caller probes at once | Own piece, after 102 | OPEN | |
| 96 | Startup WARNING when an explicit `DODEAL_REDIS_MAX_CONNECTIONS` is below `max_inflight + 4` | With 74 prep | OPEN | |

## Items 97 to 102: from the roadmap fold and the BRD, 11 September

| # | Item | Carrying step | Status | Sha |
|---|---|---|---|---|
| 97 | Per-tenant configuration store or file: mode, thresholds, weights, note types; every change versioned and dated (R28) | Before pilot (A12) | OPEN | |
| 98 | A scheduler for the daily brief (07:30 Dubai per tenant) | A6 | OPEN | |
| 99 | Unit A2a: the three per-rep measures through one read adapter (fake and real) over the backend's judgement rows | A7a | OPEN | |
| 100 | Unit A2b: the daily brief, three role versions, returned as text | A7b | OPEN | |
| 101 | `CLAUDE.md` at the repo root holding the stable session rules (R31, R35) | Hand commit, 12 Sep | Written; commit owed | |
| 102 | The flaky elapsed test on a fake clock (`time.monotonic()` ticks in 15.6 ms steps on the Windows machine) | Own piece, after 101 | OPEN | |

## Items 103 to 106: from Pieces N.4 and 103, 12 September

| # | Item | Carrying step | Status | Sha |
|---|---|---|---|---|
| 103 | The default run is hermetic with or without a Redis listening: the root conftest injects the shared fakes for every test, points the service URLs at a closed port, and fails the run naming any test that built a real pool | Piece 103 | DONE | `170ddbf` |
| 104 | M4 on the request and token Lua scripts: set the window when the key has no TTL, as the rate-limit script does; the three pinning tests change with it | A-demo, before 74 (own piece); may move to A3 | OPEN | |
| 105 | Policy-per-caller for fail-closed workers: a worker draining a backlog pauses on a cost or token guard failure instead of failing open | Step 14 (A6), with the first worker | OPEN | |
| 106 | A CI job for the `redis_real` lane with a Redis service container | With 74 prep | OPEN | |

Not applicable by decision: a dead-letter queue for Unit A; a jobs table; streaming; result caching beyond D3; prompt caching before a provider exists.
