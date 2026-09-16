# STATUS — the build register

**One file that says where the DODEAL AI service is, what has been decided, what is open, and who owes what.**
Sits beside `ASSUMPTIONS.md` (the seam ledger: what we believe about the backend and how to correct it) and does
not duplicate it. This file is about the *build*; ASSUMPTIONS is about the *contract*.

Update rule: every commit that changes a row here updates this file in the same commit. If a row's status
and the tree disagree, the tree is right and this file is wrong — fix the file.

**Last updated:** 15 Sep 2026, after Unit A Project 1 phases A–J, Piece K, Piece L, Piece M, Piece N (N.1, N.2, N.3, N.3b, N.4), Piece 103, Piece N.4b, Piece 76.1, Piece 102, Piece 95, Piece 85, Piece 86, Piece 87 and Piece 88 — the judgement pipeline is built end to end against `FakeLLM` and the vendored fake CRM, and the LLM seam now has a real Groq/OpenAI adapter that has never been called (head of `scaffold/core-governance-homes`, CI green). **Nothing in Unit A has been verified against a real model or a real backend; nothing is marked `[V]`** (ASSUMPTIONS §8.11). Redis is the one dependency that has met a real server: the `redis_real` lane (N.4), by hand, against Redis 7.4.10.

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
| Step 3 — token counters. **N.1 (`dffeb80`)**: Redis timeouts, bounded pools, `/ready` reports db2, the fakeredis lane. **N.2 (`77df41d`)**: `enforce_token_cost`, the real fail-open `token_preflight` (the no-op stub and its grep marker are gone), `TokenBudgetExceeded` 429, the warning ratio. **N.4 (`44e9071`)**: the real-Redis lane. **N.4b (`ffbf9b0`)**: the dependency audit on the locked set — the last piece of step 3. **Still owed as items 104 and 105:** the M4 TTL fix on the request and token scripts (104) and policy-per-caller for fail-closed workers (105); degradation behaviour at the warning ratio (A9) is owed as well and is not numbered with them. The H3 breaker, listed here as owed until now, landed in N.3 (`3199e3d`). | PARTIAL | `dffeb80` · `77df41d` |
| **Piece N.3b** — hardening after N.3, one commit per register item, in this order: a breaker cannot wedge in HALF_OPEN (80); pool exhaustion is not a store failure and the pool is sized from `max_inflight` (81); one deadline per judgement, 503 `judgement_deadline_exceeded` (83); the reservation is released on any exit, short while in flight and long once judged (82) | DONE | `73cf893` · `7a8c48c` · `83bc11b` · `8ba3024` |
| **Piece N.4** — the real-Redis lane (register item 3, closed; the lane half of item 29): `tests/redis_real/` runs the three Lua scripts, the reservation's `SET NX`/`XX` and `DEL`, the pool's refusal and the breaker's reaction to a dead host against a real server, marked `redis_real`, skipped without `DODEAL_REDIS_REAL_URL`, excluded from the default run. Tests and docs only, no `src/` change | DONE | `44e9071` |
| **Piece 103** — the default run is hermetic, Redis running or not (register item 103, from N.4's finding). The root `tests/conftest.py` hands every test a fresh `FakeCostRedis` and `FakeOperationalRedis` through every name the service's code calls (`limiter`, `state`, `main`), with both breakers reset. It points both service URLs at a closed loopback port for the session, and fails the run if any test outside `tests/unit/test_redis.py` builds a real pool. Tests and docs only, no `src/` change | DONE | `170ddbf` |
| **Piece N.4b** — the dependency audit on the locked set (register item 29, closed; the `pip-audit` half). `pip-audit` is a locked dev dependency, and a second CI job `audit`, beside `checks`, exports the lock and runs `uv run pip-audit --strict --desc -r audit.txt`. It audits the **exported lock, never the venv** (ruling R38): the editable project is not on PyPI, can never be resolved, and `--strict` counts that skip as a failure. A separate job because the result is a function of the advisory database, not of the commit. Zero findings at `fcdd091` across 83 pins. The seven steps of `checks` are untouched; no `src/` change | DONE | `ffbf9b0` |
| **Piece 76.1** — the OpenAI-compatible adapter on the LLM seam (register item 76, part 1 of 3). `core/llm/openai_compatible.py`: `OpenAICompatibleClient(base_url, model, api_key, http, *, settings)` satisfying `LLMClient`, **one class for Groq and OpenAI** parameterised by base URL (R16), with every conformance assertion parametrised over both URLs. JSON mode mandatory; the profile resolved inside (R17) with the ceiling only ever lowered; a **0–2 temperature bound** and a refusal of any profile naming another vendor, both enforced at resolution before the transport is entered; 401/403 auth, 429 and 5xx transient, 400 invalid request, any other 4xx unknown-and-not-transient; `stop`/`length`/`OTHER`; non-JSON and every missing field transient; `httpx.TimeoutException` to the seam's transient error and **never a builtin `TimeoutError`** (catch-list 55); **no retry** — the transport is asserted entered exactly once on every failure path. `OpenAICompatibleError` adds the status and provider name and nothing else; `str()` is still the seam's fixed reason code. `build_llm_client(settings, http)` in `core/llm/__init__.py` returns it for `groq`/`openai` and raises `ConfigError` naming the provider for `anthropic`, `gemini` and a missing key. `get_llm_client()` is unchanged and still raises — lifespan owns the pooled client in 76.2 / item 84. **No provider has been called.** | DONE | 38d60a0 |
| **Piece 76.1a** — two defects found by reviewing 76.1's own explainer. **(1)** The adapter's `httpx.Timeout` and the watchdog's `asyncio.wait_for` were both set to `llm_timeout_seconds`; the watchdog's clock starts before `_resolve()` and httpx's `read` clock only after connect and send, so the watchdog **always** won and the adapter's timeout branch was dead code — every provider timeout arrived as a bare `TimeoutError` with no provider named. The HTTP call now gets `_HTTP_TIMEOUT_SHARE` (0.9) of the budget: a share rather than a subtraction, so the margin cannot invert at a small timeout. `llm_timeout_seconds` remains the true outer bound. **(2)** `provider_reason` and `provider_transient` are now structured fields on `judgement_model_unavailable` — the reason already rode inside the `error` string, but `transient` was set on every branch and read by nothing. No behaviour change: one 503 for every provider failure, and a 429 is still never retried. | DONE | 13ffdf9 |
| **Piece 76.1b** — `.env.example` stays in step with `Settings`. It had drifted: `DODEAL_LLM_PROFILES` entered `Settings` in Piece M (`b263c8b`) and never reached the example, joined by `DODEAL_LLM_API_KEY` and `DODEAL_LLM_BASE_URL` from 76.1 — three undocumented settings, because a session may not write that file and each reported row has to be pasted by hand. `ASSUMPTIONS.md`'s claim that the file was "audited field-by-field against `core/config.py::Settings`" was true when written and silently false at the next field. `tests/test_env_example_matches_settings.py` now fails when `Settings` gains a field the example lacks, or keeps a row for a field that is gone, with a named exemption for `DODEAL_REDIS_REAL_URL` (the `redis_real` lane reads it directly, the service never does) and a fourth test that fails when an exemption stops being needed. The drift tests compare **key names only**; one test reads values — the secret-shape check — and compares without printing, which is what lets a session commit a file the permission rule forbids it to read. The three rows were pasted by the lead. `CLAUDE.md`'s `.env.example` rule was removed by the lead in the same commit. | DONE | 6dcd159 |
| **Piece 76.2** — the LLM client built once in `lifespan` (register item 84, closed; item 76 part 2). `get_llm_client(request)` READS `app.state.llm` and never builds — one pool per process, and building in the dependency would re-run the profile sweep on every judgement. Lifespan builds one `httpx.AsyncClient` sized `max_inflight × LLM_CALLS_PER_JUDGEMENT` = **64** (against passes, not requests: vague and score are gathered) plus the LLM client, and closes the HTTP client before the two Redis pools. **Permissive startup, strict readiness:** an unset provider starts the app, leaves `app.state.llm` as `None` and logs one loud `llm_not_configured` ERROR on the `dd_api_keys` precedent, while a provider that IS set and anything else wrong refuses to start. The **startup profile sweep** lives inside `build_llm_client`, so every configured profile is resolved through the adapter's own guards once at boot. `/ready` is 503 whenever no usable client exists — the defect item 84 exists to close — while the Redis fields keep their fail-open 200. `scripts/model_smoke.py` dry-runs by default, needs `--live` for a paid call, and exits with a named error without a key. **The induced-timeout run proves 76.1a on a real socket**: at share 0.9 the failure arrives in 4.52s as `OpenAICompatibleError` with the adapter's log line firing; reverted to 1.0 it arrives at 5.00s as a bare `TimeoutError` with no provider and no log line. No real provider has been called. | DONE | de0080c |
| **Piece 103b** — the default run is hermetic against a populated `.env` (register item 115, the lead's, closed). `Settings` declared `env_file=".env"`, so every `get_settings()` in the suite read whatever the developer had configured locally; the suite was green only for people who had configured nothing. Configuring `.env` to make a live provider call turned **six tests red**, two of which (`test_dd_api_keys_parses_json_map_of_secrets`, `test_backend_keys_missing_logs_error_when_map_is_empty`) predate item 76 entirely and would have broken for anyone at any point in the campaign. **`monkeypatch.setenv` could not have fixed it:** for a DICT field pydantic-settings MERGES sources, so a test that set `DODEAL_DD_API_KEYS` by hand still received the file's entries on top — it asserted `{'t1','t2'}` and got `{'localtenant','t1','t2'}`. `_no_dotenv` in the root conftest clears `env_file` session-wide, the way `_closed_redis_urls` points both Redis URLs at a closed port. Four guard tests in `tests/test_hermetic_fakes.py` assert the property rather than assume it, including the dict-merge case and that the `_build_settings(_env_file=...)` seam still works. Suite identical with `.env` present and with no `.env` at all: **1218 passed** both ways. Tests and docs only, no `src/` change. | DONE | b263175 |
| **Piece 76.2a** — the smoke script prints the failure it already has. `scripts/model_smoke.py` printed `FAILED: ModelUnavailableError` and told the operator to "check DODEAL_LLM_API_KEY, the model id, and the provider's status page" — three guesses, and three debugging rounds were spent on them. The adapter had already recorded which of the three it was: `_error` puts `reason_code`, `provider`, `status` and `transient` on the `llm_provider_call_failed` record. The script now captures that record for the duration of the call and prints the four fields plus **one** next action keyed on status (401/403 the key, 404 the model id, 400 the body, 429 the limit, 5xx the provider, no-status a timeout/DNS/refusal). `<<missing>>` where a field is absent, so absence reads as a fact. Proven end to end against a real local server at each of 400/401/403/404/429/500, with a sentinel planted in every response body appearing in no run's output — no body, header, URL or key is ever printed. `--induce-timeout` now shares the same capture context. No `src/` change; the script is not collected by pytest. | DONE | a3c8c48 |
| **Piece 78** — the demo runtime (register item 78, closed). `DODEAL_BACKEND_SCHEME`, a `Literal["https", "http"]` defaulting to `https`, replaces the `https://` literal in `LeadsClient._base_url` — the one place in `src/` that names a scheme. It exists because the demo serves a fake CRM from a laptop, which has no certificate for `tenant-a.dodealcrm.com`; it is **demo-only**, it does **not** disable certificate verification, and `http` is set in exactly one committed file. `.env.demo` is that file: every value in it invented, committed behind a `!.env.demo` negation, and deliberately **outside** the 76.1b guard, which needed no change — it is pinned to `.env.example` by path. `scripts/mint_demo_token.py` mints a token that passes Gate 1 and Gate 2 and prints the `curl` line with the `Host` header Gate 2 requires; it makes no network call, never prints the signing key, and builds `Settings` over the same `.env` → `.env.demo` stack Compose gives the container, so it cannot sign with a key the service does not verify with. `docker-compose.demo.yml` is an override, not a replacement: `.env.demo`, an `extra_hosts` line resolving `tenant-a.crm.demo.invalid` to the Docker host, and a Redis healthcheck the API waits on so the first judgement cannot meet a fail-closed idempotency store. `docker-compose.yml`'s `.env` became `required: false` so a clean checkout comes up at all. **The demo cannot produce a judgement until the lead sets the three `DODEAL_LLM_*` rows**, which this piece deliberately does not choose. | DONE | ca22b2f |
| **Piece 78a** — the two runtime guards Piece 78's explainer named and did not build. **No register item**: it comes from 78's own Production Failure Modes. **(1)** `lifespan` logs `backend_scheme_insecure` at ERROR when `backend_scheme` is `http`, beside `backend_keys_missing` and `llm_not_configured` and after `configure_logging()` for the §6.9 reason. Never a refusal — the demo needs `http` — and it names no key, no tenant and no URL, asserted by a test that sets a real-looking key, a tenant and a domain and proves none of the three reaches the message. **(2)** `tests/test_demo_env_stack.py` pins the `env_file:` order Compose builds across both compose files to `scripts/mint_demo_token.py`'s new `DEFAULT_ENV_STACK`, which replaces `_BASE_ENV_FILE` and the argparse literal as the ONE definition of that stack. A divergence would make the minter sign with a key the service does not verify with and surface as a generic `401 invalid_token` that reads like a broken gate. **No YAML parser**: `pyyaml` is here only transitively (`uvicorn[standard]`, `pre-commit`) and nothing declares it, so the pin is a fail-closed line scan with a tripwire that fails rather than emptying. | DONE | 5cc1e55 |
| **Piece 102** — the flaky elapsed test on a controlled clock (register item 102, closed). `test_elapsed_covers_more_than_any_single_pass` drove `elapsed_ms` with a real `asyncio.sleep(0.02)` and asserted the outcome line carried at least 20 ms. `time.monotonic()` advances in **~15.6 ms steps** on the Windows machine, so a **correct** pipeline measured 15 and the assertion failed at random — observed 1 in 6 on one head and 5 in 14 during a later piece. `_FakeClock` replaces the pipeline module's `time` global and the fetch wrapper **advances** it by 20 ms instead of sleeping. The module global rather than `time.monotonic` itself, which is the precedent in the same file: that attribute is shared with asyncio's event loop, so a clock that jumps under the loop's own scheduling swaps one source of flakiness for another; every other attribute delegates to the real module. **Both assertions are unchanged** — `elapsed_ms >= 20` and `classify_ms < 20`, nothing weakened to `>=`, no tolerance margin, either of which would let the real defect back in. Twenty consecutive module runs, twenty green. Item 72's elapsed fields are untouched: **no `src/` change**, and the sabotage proves the assertion still bites. | DONE | `498d937` |
| **Piece 95** — a pool-refused probe no longer holds the breaker half-open (register item 95, closed). `CircuitBreaker.call` skipped `_record_failure` for `PoolExhausted` and did nothing else, so a probe our own pool refused left the state **HALF_OPEN with the probe slot held**: every other caller met `BreakerOpen` for a full `open_seconds` until guard 2's abandoned-probe takeover — on db2, a 503 per judgement, and the trigger a busy pool rather than a store that is down. Item 81 removed that cost in CLOSED; this removes it in HALF_OPEN. The branch now reverts the state to OPEN and **leaves `_opened_at` exactly as it was**: that window has already elapsed, so `_admit` promotes the next arriving call to the probe at once, with no clock advance needed. **Not `_open()`** — it re-stamps `_opened_at` from the clock, imposing a fresh full window and costing more than the defect; the sabotage proves it (`assert 1030.0 == 1000.0`, and the next caller refused). `PoolExhausted` is still re-raised unchanged and still never counted, in any state, so each caller's own policy decides that one call. The existing `breaker_probe_abandoned` line is reused rather than a new event name added: the named-events list is a documented surface. Three tests; the guard 2 takeover test passes unchanged. | DONE | `cf86985` |
| **Piece 85** — the nine prompt templates are read once at startup (register item 85, closed). `_load_template` read from disk inside `build_prompt` and `with_tail`: a **synchronous read on the event loop**, three to six per judgement, and a template missing from the image was discovered on the **first paid call** — after classification had already been bought. `core/prompting.py` gains `_TEMPLATE_CACHE`, `preload_templates(names)` and `clear_templates()`, and the lifespan calls the first **immediately after `configure_logging()`, before `app.state.http` exists**, so a missing file refuses startup before any socket is opened and the piece adds no unclosed resource. The list is the unit's, not core's: `units/structured_intelligence/templates.py` builds `UNIT_A_TEMPLATES` from `CLASSIFY_TEMPLATE`, `template_for()` over the six vague-checked `NoteType` members, `SCORE_TEMPLATE` and `REPROMPT_TAIL_TEMPLATE` — the constants themselves, never retyped strings, so a renamed file cannot leave a sent template out of the preload. **The disk fallback stays**: an uncached name reads exactly as before, which is what keeps `scripts/verify_wheel.py`, `tests/unit/test_assembled_prompt.py` and every `_prompts_dir`-to-`tmp_path` test green unchanged. The refusal names the **template** and not the resolved path, which would carry the deployment's directory layout into a log line. Preload is all-or-nothing, so a caught `PromptError` leaves no half-filled cache. Seven tests; one full judgement with a reprompt makes **zero** reads. | DONE | `f3e3788` |
| **Piece 86** — the two middlewares as pure ASGI (register item 86, closed). `RequestIDMiddleware` and `InflightMiddleware` were both `BaseHTTPMiddleware`, which runs **every** request through an anyio task group and a pair of memory object streams so `dispatch` can be handed a `Request` and return a `Response`. On the load-shed path that is the defence costing more than the work it refuses — the middleware's own docstring says a refusal must be "a counter comparison and nothing more", and it was paying for a task group first. It also ran the route in a **child task**, so a `contextvars.ContextVar` set in a route was invisible to anything outside these two (proved both ways: with them in the stack an outer middleware read the default, without them it read the route's value). Both are now `__init__(app)` plus `__call__(scope, receive, send)`. **Behaviour, order, log lines, response bodies and headers are unchanged**, and every existing test — the twenty-concurrent exactness test, the `finally` release on a raising route, `app.state.inflight`, and the security tests on injected and overlong ids — passes **unedited**; that is the evidence. The refusal `Response` is now awaited as an ASGI app rather than returned, which is the same object and the same bytes. Two things are genuinely new: a `lifespan` or `websocket` scope passes straight through **uncounted** (counted, a lifespan scope holds its slot until the process exits and the cap is one lower for the life of the pod), and the id header is appended to a **copy** of the `http.response.start` headers, never to the inner app's list. `tests/test_no_base_http_middleware.py` keeps the base class out, with patterns import-shaped and class-shaped so both docstrings can still explain why they are not one. | DONE | `76ed6af` |
| **Piece 87** — the body-size limit, outermost (register item 87, closed). Nothing in the app bounded a request body: a caller could send megabytes and the app read every one of them into memory **before Gate 1 decided whether that caller was allowed to send anything at all**. `middleware/body_limit.py` is a third pure-ASGI middleware and the **last** `add_middleware` call in `main.py`, so it is the outermost layer — a body over `DODEAL_MAX_REQUEST_BODY_BYTES` (64 kB, `ge=1`) is **413 `payload_too_large`** before the request id, before load shedding, before routing and before all four gates, proved by an oversized request with **no token at all** answering 413 rather than 401. Two paths: a `content-length` above the cap is refused **without calling `receive` once**, and a body that is chunked, unlabelled or **lying** about its length is counted message by message and refused at the byte it passes the cap, the count an integer and never a copy. Exactly the cap is admitted. **The middleware sends both 413s itself** rather than raising to the `DodealError` handler, because FastAPI re-raises every exception out of `await request.body()` as its own `HTTPException(400, "There was an error parsing the body")` — measured, not assumed — so a delegated refusal would tell the caller its request was **malformed** when it was only too big; the wrapped `receive` raises to stop the read and the wrapped `send` discards whatever the inner app answered with, unless the response has already started, which ASGI does not let anyone retract. The header-path 413 carries `request_id` `"unknown"` (nothing has minted one yet) and the streamed one carries a real id (by then the id middleware has run). It logs **nothing**: a `WARNING` per oversized request is a log-volume lever a caller controls. `/health` and `/ready` are **not** exempt, unlike in `inflight.py` — they carry no body, and an exemption is a path a caller can aim a large body at. The cap is read **per request**, never in `__init__`, which is the regression ASSUMPTIONS §10.1 was written about; that section is rewritten here from "not coming back" to what was built. | `39018b5` |
| **Piece 88** — `JsonFormatter` formats `exc_info` frames only (register item 88, closed). No log call inside the `dodeal_ai` tree passes `exc_info`, and the catch-all in `core/errors.py` logs frames by hand — so the hole was **outside** the tree: starlette's `ServerErrorMiddleware`, uvicorn's error logger and any library in the process log an exception with `exc_info=True` through **our** root handler, and the formatter rendered that with `self.formatException`, which prints `str(exc)` **and every message in the `__cause__`/`__context__` chain**. A foreign exception's message is frequently the data that failed: a note body, a model completion, a backend error string. The branch now emits `exc_type` (module-qualified class, the two halves `safe_error_fields` reports separately, joined) and `exc_frames` (`log_safety.frames_only`), and **the `exc_info` key is gone** — a collector query on it matches nothing and is corrected rather than surprised. `str()` is never called on the value anywhere in `format`; `record.exc_text` is never read, because that is where the stdlib caches the message-bearing text when a record is formatted twice. Only the tuple shape is handled: `(type, None, None)` yields the type and no frames, a bare `True` yields neither field, and a formatter that raised would lose the line entirely. `record.stack_info` was ignored before and is ignored now, said so in the docstring. `tests/security/test_log_exc_info.py` is the stdout-wide guard: a foreign `RuntimeError` whose message is a sentinel, chained from a second sentinel, raised in a test-only route on the real app (registered by the fixture, removed on teardown, never in `src/`), captured with **`capfd`** — the bytes on the file descriptor, not `caplog` — under the real `configure_logging()`. Neither sentinel reaches stdout, stderr or the 500 body; `exc_frames` still names the failing route function. Every existing test in `tests/security/test_log_safety.py` passes **unedited**; none asserted the old key by name. | DONE | `77a2bbd` |
| Step 4 — tool layer: query params, paging, error taxonomy, retry policy, pooled transport, per-item validation. **Now also: read-after-write bounded re-read on the note fetch (candidate — see §2 debts).** | after step 3 | — |
| Steps 5–13 | per ed3 §15 | — |
| Step 0 — test tenant + key + joint call | BLOCKED on backend (Waqas) | — |
| Step 14+ | BLOCKED on step 0 | — |

Suite at head: **1288 tests passing, 1 skipped, 30 deselected; 99.59% coverage**, identical with the compose Redis running or stopped (Piece 103). Piece 76.1 adds 115 tests (`tests/unit/test_openai_compatible_adapter.py`) and takes `core/llm/openai_compatible.py` to 100%. Total floor 92, plus 15
per-file floors on the deny-path and judgement modules — `limiter.py` and `core/breaker.py` at 100 against
floors of 100, and `pipeline.py` and `state.py` at 100% against floors of 95),
ruff/format/mypy clean, wheel installs and imports in a clean venv. The skipped test is `tests/eval/test_quality_eval.py`, which needs a real model (step 18);
the deselected 30 are the `integration` marker (9) and the `redis_real` lane (21). Piece 86 adds 16 tests (`tests/test_no_base_http_middleware.py`, `tests/unit/test_middleware_asgi.py`) and leaves both middleware modules at 100%. Piece 87 adds 25 (`tests/unit/test_body_limit.py`) and takes `middleware/body_limit.py` to 100% as well, with no floor added for it. Piece 88 adds 7 (`tests/security/test_log_exc_info.py`) and takes `core/logging_config.py` to **100%**, with no floor added for it: the `exc_info` branch used to be reached only when something outside the tree happened to log one, which is the flip that moved the total between 99.51% and 99.57%. Three full runs at `77a2bbd` all read **1288 passed, 1 skipped, 30 deselected, 99.59% (99.5885%)**, with `core/logging_config.py` at 100% and no missing lines in any of them — the flip is gone. The lane's own run, by hand against
Redis 7.4.10: **21 passed** (N.4).

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
| 3 (step 3, first sub-commit) | Redis timeouts and bounded pools; `/ready` reports db2; the fakeredis lane | Four `DODEAL_REDIS_*` budget settings, all `gt=0` so a non-positive value is a `ConfigError` at construction. `core/redis.py` builds a bounded `BlockingConnectionPool` per named connection from those four and holds **no numeric literal at all** — a parse-the-module test fails the build if one returns. `/ready` gains `operational` (db2) beside `redis` (db1), same `PING` probe under the same socket timeout, same `200`-with-`degraded`; missing config is still `503`. `fakeredis[lua]` is a dev dependency and `tests/unit/test_cost_lua.py` executes the real Lua script. A `redis_real` marker is registered and excluded from the default run; nothing carried it until Piece N.4. | `dffeb80` |

### Register items advanced in Piece N.2

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 24 | Token counters and the real pre-flight | `tokens:tenant:{t}` and `tokens:user:{t}:{s}`, moved by a SECOND Lua script that shares no key with the request script. `enforce_token_cost(scope, input_tokens, output_tokens, profile)` charges once per model response that reports usage, from `llm_call.complete_once` — the one point every paid call passes through, so a reprompt's discarded first answer is charged too. It logs `tokens_charged` (INFO) per call and `token_charge_bypassed` (WARNING) on a Redis outage, and it never denies: the call it charges for is already paid. `token_preflight` is a real MGET of both keys, raising `TokenBudgetExceeded` (**429 `token_budget_exceeded`**) at or above either limit and failing open with a once-per-process `token_preflight_bypassed`. The stub and every `SEAM` marker naming it are gone, and `tests/test_assumption_markers.py` now asserts the marker's **absence**. | `77df41d` |
| 61 (design half) | The token-budget warning ratio | `cost_token_warning_ratio` (0.9, `gt=0` `lt=1`) and one `token_budget_warning` WARNING the first time a running total crosses `limit × ratio` within its window — fired once per crossing by comparing the pre-call and post-call totals, with no second key and no in-process flag. **The design half only.** Nothing degrades at the ratio; what should happen there is A9's decision and is deliberately not made here. | `77df41d` |

**Items 24 and 61 could not be located in this repo either** — the same gap N.1 reported for item 25. No tracked file carries a register numbered 24 or 61; both rows above are written from the Piece N.2 brief's description of them rather than from a register entry read in the tree. If the register lives outside the repo it still needs marking by hand.

**The item numbered 25 could not be found.** No tracked file in this repo has a register item 25 — not this file, not `CAMPAIGN_REPORT.md`, not `README.md`. Piece N.1 was asked to mark items 3 and 25 done; item 3 is above (partly — see the step 3 row), and 25 is recorded here as unlocated rather than guessed at. See "For the lead" in `CAMPAIGN_REPORT.md`, Piece N.1.

**Not in this piece:** register item 9 (gather the lead and notes fetches). `gather_or_cancel` is written so item 9 uses it unchanged — its two-argument form is generic and that is the shape item 9 needs.

### Register items advanced in Piece N.3

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 20 (Redis half) | Circuit breaker on Redis | `core/breaker.py`: `CircuitBreaker(name, *, failure_threshold, open_seconds, clock=time.monotonic)` with three states — closed, open, half-open with **exactly one probe**. `BreakerOpen` subclasses `redis.RedisError`, so every existing `except redis.RedisError` keeps its meaning. Two breakers, `cost_breaker()` (db1) and `operational_breaker()` (db2), built from `DODEAL_BREAKER_FAILURE_THRESHOLD` (5) and `DODEAL_BREAKER_OPEN_SECONDS` (30.0), both **provisional** and `gt=0`. Every call in `core/cost/limiter.py` and `units/structured_intelligence/state.py` runs inside one. While open, the fail-open paths take their existing bypass branch with `breaker: open` on the line, and the reservation still raises 503 `idempotency_unavailable`. `breaker_opened` / `breaker_closed` (WARNING) on each transition and nothing else. The once-per-process `token_preflight_bypassed` latch is removed: the line is logged on every bypass, and the transition pair is the de-duplication. **The provider half (A9) is not here.** | `3199e3d` |
| 27 | The rate limit in one round trip | `state.take_rate_limit` runs one Lua script: increment only while the key is under the limit, set the window on creation (and on a key with no TTL — the M4 guard, repaired for this key only), return `(allowed, count before)`. The pipeline reads the rate limit only after conditions 1 and 2: a resubmission or a note at its attempt cap makes **no** rate-limit call; a prompt that would be sent runs the script, which **is** the increment (`increment_rate_limit` and its call after `decide` are gone); no question makes one plain read, so an exhausted window still reports `rate_limited` ahead of `nothing_to_ask`. `decide()` stays pure and takes `rate_allowed` beside `rate_count`; its outputs and the withheld order are unchanged. | `3199e3d` |

**Item 3 was still partial after N.3.** N.3 closed the H3 breaker's Redis half. N.4 closes item 3 with the real-Redis lane (below). The two things this paragraph named as owed are **not** in N.4 and stay owed on the step 3 row, with no piece scheduled: the M4 TTL fix on the two cost scripts, and policy-per-caller for fail-closed workers.

**Items 20 and 27 could not be located in this repo either**, the same gap N.1 and N.2 reported. No tracked file carries a register numbered 20 or 27. Both rows above are written from the Piece N.3 brief.

### Register items closed in Piece N.3b

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 80 | A breaker cannot wedge in HALF_OPEN (CRITICAL) | `CircuitBreaker.call` counted only `redis.RedisError`. A probe that was cancelled, or that raised anything else, left the breaker HALF_OPEN with nothing to move it, and every later call was `BreakerOpen` until restart. On db2 that is 503 `idempotency_unavailable` on every judgement. Two guards now. **(1)** An `except BaseException` branch re-arms the window when it ends a probe. It logs `breaker_probe_abandoned` and then `breaker_opened`, and re-raises unchanged. In CLOSED such an exception neither counts nor clears. **(2)** `_admit` stamps `_probe_started` on entry to HALF_OPEN. A probe older than `open_seconds` is taken as abandoned, and the arriving call becomes the probe (`breaker_probe_abandoned`). This covers a probe task that is never resumed at all. | `73cf893` |
| 81 | Pool exhaustion is not a store failure; the pool is sized from the in-flight cap (HIGH) | A `BlockingConnectionPool` that could not hand out a connection within 1.0 s raised `redis.ConnectionError`, and the breaker counted it. With 32 requests in flight over a pool of 20, a latency spike opened the db2 breaker for 30 s on a healthy Redis. Now `core/redis.py` has `PoolExhausted(redis.ConnectionError)` with a fixed message, and `BoundedPool` relabels **only** the acquire timeout, recognised by redis-py's refusal being chained from the wait's `TimeoutError`. A socket that will not connect still raises its own error and is still counted. The breaker's `RedisError` branch skips `_record_failure` for `PoolExhausted`, and every caller's policy for that one call is unchanged. `redis_max_connections` is now `int \| None` (default unset). `Settings.redis_pool_size` is that value when set, otherwise `max_inflight + REDIS_POOL_HEADROOM` (4), so **36** by default. The three settings touched carry three-line comments again. | `7a8c48c` |
| 83 | One deadline per judgement (HIGH) | There was no end-to-end bound. The per-call budgets add up to about 280 s (two backend reads at 10 s with a retry each; three passes at 60 s with a reprompt each, two of them gathered), and the CRM waits inline (Q16). `judgement_deadline_seconds` (25.0, `gt=0`, **provisional until Q16**) is new. `judge_note` and `judge_note_direct` each run everything after their `time.monotonic()` inside one `asyncio.timeout`. That includes the fetch, so the timeout sits **outside** `_judge`. A `TimeoutError` becomes `JudgementDeadlineExceeded`, **503 `judgement_deadline_exceeded`** (the `model_unavailable` family, not 504), with one `WARNING` line carrying `tenant`, `request_id`, `elapsed_ms` and `inflight`. No per-call timeout was shortened. | `83bc11b` |
| 82 | The reservation is released on any exit and does not outlive the work (CRITICAL) | The reservation was taken with the tenant's long TTL before the work and released only in `except Exception`. `CancelledError` is a `BaseException`, so a deadline, a disconnect or a shutdown left the key behind. A SIGKILL skips every `finally`. Either way, every CRM retry met 409 for a day. Now `_judge` releases in `except BaseException`, with the `DEL` under `asyncio.shield` so a second cancellation cannot kill it on the wire. **Reserve short, confirm long.** `reserve_idempotency` is given `ceil(judgement_deadline_seconds × IDEMPOTENCY_INFLIGHT_MULTIPLIER)`, a multiplier of 4, so **100 s** at the default. The new `state.confirm_idempotency` runs `SET key "done" XX EX` under `operational_breaker` and **fails open** with `idempotency_confirm_bypassed`. It applies `idempotency_ttl_seconds` (**24 h**) inside the try, after the judgement is built and before `_log_outcome`. A classifier suppression confirms like a score. The length gate still reserves nothing. `_RESERVED` stays `"1"`, and `"done"` is the D3 seam: no judgement is stored. | `8ba3024` |

**Items 80–83 could not be located in this repo either**, the same gap N.1–N.3 reported. No tracked or untracked file carries a register with those numbers. The four rows above are written from the Piece N.3b brief.

### Register items closed in Piece N.4

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 3 | The bounded pool, the scripts and the breaker, proven on a real server | Until N.4, everything item 3 built had run only on fakes or stubs. The Lua scripts, `SET NX`/`XX` and `DEL` ran on fakeredis or `FakeOperationalRedis`. `BoundedPool` ran on stub connections that open no socket. The breaker ran on a fake clock and a synthetic `RedisError`. `tests/redis_real/` now runs all of it against a real server, **21 tests, green against Redis 7.4.10**, and each test names its hermetic counterpart. It covers the request, token and rate-limit scripts (counts, window on create only, M4 as it behaves today, disjoint namespaces, and exactly `limit` slots under concurrent `EVAL`s). It covers the reservation (one `NX` winner among concurrent claims, `XX` never creating a key and replacing value and TTL, `DEL` freeing the note). A pool of one refuses as `PoolExhausted` with redis-py's own cause chain. A dead host fails as a store error that is not `PoolExhausted`, opens a threshold-two breaker, and is then refused without a call. A live host clears the count. The lane skips, never fails, when `DODEAL_REDIS_REAL_URL` is unset, the server does not answer `PING`, or the URL selects db0–db2. The conftest marks the whole directory `redis_real` by path. The default run deselects it, and the lane is a hand run (README, "The real-Redis lane"). | `44e9071` |
| 29 (lane half) | A real-Redis test lane | The lane itself and its documented hand run. The CI half is not here: no workflow runs it. That is now register item 106, owed with the 74 prep. | `44e9071` |

**Items 3 and 29 as the N.4 brief numbers them.** Item 3 is the N.1 row above. This file previously listed the M4 TTL fix and policy-per-caller under item 3. Neither is in N.4; they are now register items 104 and 105 and remain owed on the step 3 row. Item 29 could not be located when N.4 ran; `docs/register.md` is tracked as of `9f6920e` and carries it, and **item 29 is closed by Piece N.4b below**.

### Register item closed in Piece 103

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 103 | The default run is hermetic, Redis running or not | N.4 found that the default suite reached a real `redis.asyncio` client, and so passed only with no Redis listening. Eight modules reach it. Six do so through the token charge in `complete_once` (`test_vague`, `test_reprompt`, `test_classification`, `test_scoring`, `test_fake_llm`, `test_log_safety`), and two through the app lifespan's `aclose()` (`test_logging_config`, `test_startup`). With the compose Redis up, 42 tests failed with `Event loop is closed` and the charge wrote to `tokens:*` in db1. The root `tests/conftest.py` now does three things. **(1)** `redis_fakes`, autouse, gives every test a fresh `FakeCostRedis` and `FakeOperationalRedis` through `limiter.get_cost_client`, `state.get_operational_client` and both names on `main`. It clears both factory caches and resets both breakers, and a module's own patch still wins. **(2)** `_closed_redis_urls`, session-scoped, points `DODEAL_REDIS_COST_URL` and `DODEAL_REDIS_OPERATIONAL_URL` at `127.0.0.1:1`. **(3)** `_real_pool_builds` wraps `core/redis.py`'s `_build_pool`, and `pytest_sessionfinish` fails the run if any test outside `tests/unit/test_redis.py` built a real pool. `tests/test_hermetic_fakes.py` gains three tests: every src module that imports a factory by name is replaced; the service URLs point at the closed port, and the lane reads its own variable; and a module's patch wins. The two helper fakes gain `aclose()`. The `redis_real` lane gets no fakes. Tests and docs only, no `src/` change. The run numbers, `DBSIZE` and the sabotage record are in `CAMPAIGN_REPORT.md`, Piece 103. | `170ddbf` |

**Item 103 was new with the Piece 103 brief** and had no register entry in any tracked or untracked file at the time. `docs/register.md` is tracked as of `9f6920e` and now carries 103 and 29; the items still unlocated are 20, 24, 25, 27, 61 and 80–83.

### Register item closed in Piece N.4b

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 29 (`pip-audit` half) | Dependency audit on the locked environment, run in CI and by hand | The third and last half of item 29; `fakeredis[lua]` (`dffeb80`) and the real-Redis lane (`44e9071`) were the other two, so **item 29 is now closed**. `pip-audit>=2.10.1` is a locked dev dependency — `uv.lock` goes from 63 to 84 packages, +21 for the auditor's closure, including `requests` and `urllib3` in the dev group only. A second CI job, `audit`, sits beside `checks` and runs `uv export --format requirements-txt --no-emit-project -o audit.txt` then `uv run pip-audit --strict --desc -r audit.txt`. Three things are deliberate. **(1)** It audits the **exported lock, never the venv** (ruling R38, from this piece's Phase 0): `uv sync` installs the project editable, it is not on PyPI, `pip-audit` cannot resolve it, and `--strict` treats that skip as fatal — so `uv run pip-audit --strict` against the venv fails on every push forever, and `--skip-editable` does not rescue it because `--strict` counts a deliberate skip too. **(2)** It is a **separate job**, not an eighth step in `checks`, because its result is a function of the advisory database rather than of the commit: a newly published advisory must not turn the required chain red on a hotfix that changed no dependency. **(3)** `--strict` stays, so a third-party pin the auditor cannot check fails the job instead of passing silently; there is no `continue-on-error` and no `--ignore-vuln`. **Zero findings at `fcdd091`**, exit 0 under `--strict` across 83 pins — which also proves nothing was skipped. The audit cannot be proven by a repo test, so the guard is external and is recorded in `CAMPAIGN_REPORT.md`, Piece N.4b. No `src/` change. | `ffbf9b0` |

### Register item closed in Piece 95

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 95 | A half-open probe refused by the pool reverts to OPEN without refreshing `_opened_at` | The gap N.3b's "For the lead" item 2 recorded and left open. `PoolExhausted` is a `redis.ConnectionError`, so `call`'s `except redis.RedisError` clause matches it first and the `except BaseException` guard (item 80's guard 1) never sees it — a probe the pool refused is not one that failed to **answer** but one that never **started**, so neither existing guard fired. The branch now logs `breaker_probe_abandoned` and sets `_state = OPEN` directly, with `_opened_at` untouched, then re-raises. Leaving the stamp is the whole point: `_admit` promotes OPEN → HALF_OPEN on `now - _opened_at >= open_seconds`, and that window elapsed before this probe was admitted, so the next call probes immediately. `_open()` would re-stamp it and refuse everyone for a fresh window — strictly worse than the defect. What must **not** change is asserted too: in CLOSED a pool refusal is still uncounted, still silent, and still neither raises nor clears the consecutive count. The module docstring gains the third path under the pool paragraph; item 80's two guards are **not** renumbered, because they answer a different question. No new event name, no setting, no change to `core/redis.py` or any caller. | `cf86985` |

### Register item closed in Piece 85

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 85 | Preload all nine prompt templates at startup; a missing file fails startup | **Closed.** The nine are read once by the lifespan, before any socket or pool is built, and held in a module-level cache in `core/prompting.py` for exactly the app's life — `clear_templates()` is the first step after `yield`, because clearing a dict cannot fail and so cannot be skipped by an earlier shutdown error. A judgement now assembles five prompts (classify, vague, the vague reprompt, its tail, score) with **no disk read on the event loop**. `preload_templates` builds a local mapping and publishes it only once every name resolved, so a refused startup leaves no partial cache for a caller that caught the error. `UNIT_A_TEMPLATES` lives in the **unit** (`units/structured_intelligence/templates.py`), not in `core/`: core knows how to read a template and has never known which ones exist, and the list is built from the four constants the calling modules already pass to `build_prompt`. The six vague names come from iterating `NoteType` minus `SYSTEM_EVENT`, so an eighth note type joins the preload the moment it is added and a template missing for it is an import-time `PromptError` rather than a `KeyError` on a paid call. **Not changed:** the disk fallback for an uncached name, `prompts/unit_a_v1.txt`, the wheel check, and the lifespan's existing order (recorded in `CAMPAIGN_REPORT.md`, Piece 85, "For the lead"). | `f3e3788` |

### Register item closed in Piece 86

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 86 | Rewrite `RequestIDMiddleware` and `InflightMiddleware` as pure ASGI | Both classes are now `__init__(self, app)` plus `async def __call__(self, scope, receive, send)`; `main.py`'s registration, the order and the two class names are untouched. `RequestIDMiddleware` reads the header out of `scope["headers"]` (lower-cased bytes, decoded latin-1, first occurrence wins, exactly as `Headers.get` did), validates it against the unchanged `_VALID_REQUEST_ID`, and writes `request_id` and `observability` into `scope["state"]` — the dict `request.state` is a **view over**, so all four `getattr(request.state, "request_id", "unknown")` consumers keep reading it with no change of their own. The id is appended to a **copy** of the `http.response.start` header list: a `Response` sends its own `raw_headers` object and can send it again, so an in-place append would add one id per send. `InflightMiddleware` publishes the counter through `scope["app"].state`, compares `scope["path"]` against `EXEMPT_PATHS` (`URL.path` **is** `scope["path"]`, so the match is unchanged), and awaits the refusal `Response` as an ASGI app — same status, same headers, same body bytes, from the same `dodeal_error_response` builder. The `finally` release (item 73) survives and now guards a **directly propagating** exception rather than one `call_next` re-raised. New: a non-http scope passes through untouched and uncounted in both. | `76ed6af` |

### Register item closed in Piece 87

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 87 | Pure-ASGI body-size limit, outermost, 64 kB, 413 above it, before Gate 1 | **Closed.** `middleware/body_limit.py`: `BodyLimitMiddleware`, `__slots__ = ("app",)`, no settings read in `__init__`, the cap read per request from `get_settings().max_request_body_bytes`. `core/config.py`'s commented placeholder becomes a real `max_request_body_bytes: int = Field(default=65_536, ge=1)` and `.env.example` gains `DODEAL_MAX_REQUEST_BODY_BYTES` beside `DODEAL_MAX_INFLIGHT`. `core/errors.py` gains `PayloadTooLarge` (`payload_too_large`, 413) — 413 and not 503 because this is about **this request's size**, not the service's capacity (`LoadShed`) and not the caller's quota (Gate 4). `main.py` registers it last, so it is outermost, and the order comment and the dead `# register_body_size_limit(app)` line are corrected. **One deliberate departure from the prompt, recorded in `CAMPAIGN_REPORT.md`:** the prompt specified that the streamed-path exception would reach the `DodealError` handler and come back as 413. It does not — FastAPI 0.140.13 wraps every exception out of `await request.body()` into `HTTPException(400)`, measured against the real stack before any code was written, so the caller would have been told 400. The middleware therefore owns its refusal end to end. The status, the reason code, the body shape, the placement and the cap are all exactly as item 87 specifies. | `39018b5` |

### Register item closed in Piece 88

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 88 | `JsonFormatter` formats `exc_info` frames only; a stdout-wide sentinel test through the ASGI stack | **Closed.** `core/logging_config.py` imports `frames_only` from `core/log_safety.py` (which imports nothing from `dodeal_ai`, so the direction is one way) and the `exc_info` branch became `exc_type` + `exc_frames`, the old key removed. The formatter does not call `safe_error_fields`: that function also emits the message for `dodeal_ai` classes, and a formatter must not know which classes are ours — so the spelling is its two type fields joined, `f"{__module__}.{__name__}"`. Guarded by `tests/security/test_log_exc_info.py` (7 tests, `capfd`, the real `configure_logging()`, a test-only route on the real app). Sabotage: restoring `self.formatException` fails 5 of the 7, the chained one on the INNER message; adding `str(exc)` to `exc_type` fails the same 5 on the outer. | `77a2bbd` |

### Register item advanced in Piece 76.1

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 76 (part 1 of 3) | Provider adapters; `LLM_API_KEY`; JSON mode; status and finish-reason mapping; per-provider temperature bound; conformance tests on `MockTransport` | **Part 1 closes the `OpenAICompatibleClient` half.** `LLMProvider` gains `GROQ`, `OPENAI` and `GEMINI` — membership means "the name parses", not "an adapter exists", so `build_llm_client` refuses the two without one **by name**, which is a clearer failure than a rejected enum. `DODEAL_LLM_API_KEY` is a `SecretStr` read in exactly one place, the bearer-header builder; `DODEAL_LLM_BASE_URL` is a proxy override only and is never logged or carried on an exception, because a proxy URL's host or path can itself be the credential. The two vendor base URLs are constants in the adapter module, not settings: they are facts about a vendor's API rather than a per-deployment choice. **One class for both vendors** (R16) with the conformance suite run against both URLs, so a divergence fails a test rather than a deployment. Two guards run at resolution, before any paid call: the 0–2 temperature bound (per-provider, which is why it is not on the shared `ModelProfile` field, capped at 1.0) and a refusal of a profile naming a different vendor, which would otherwise post that vendor's model id to this vendor's URL. **Still open in 76.2 and 76.3:** lifespan wiring and `/ready` (with item 84), `scripts/model_smoke.py` and the live Groq run, and `GeminiClient`. | 38d60a0 |

### Register item closed in Piece 76.2

| # | Item | What landed | Commit |
| --- | --- | --- | --- |
| 84 | Build the LLM client once in `lifespan`; fail startup on `ConfigError`; `/ready` fails closed without it | **Closed.** Split deliberately into permissive startup and strict readiness. "Fail startup on `ConfigError`" is satisfied where it means something — a provider that IS configured and is wrong (no key, no adapter, no model, a profile that fails the sweep) refuses to start — while an UNSET provider starts the app and logs one loud `llm_not_configured` ERROR, on the precedent `main.py` already set for `backend_keys_missing`: no provider is provisioned yet (Step 0), and a service that refuses to start cannot serve `/health`, `/ready` or the gate chain. `/ready` then carries the fail-closed half: 503 whenever no usable client exists, so a pod that cannot judge never joins rotation. That is the defect this item exists to close, and a 200 on an unconfigured pod would have left it open. **No LLM config was wired into any test fixture**, so the seven lifespan files are untouched. Folded in without an item of its own: the **startup profile sweep**, inside `build_llm_client`, resolving every configured profile through the adapter's guards once at boot instead of 503ing the first judgement that names a bad one. | de0080c |

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
| H3 | Redis outage costs 2.0s per request in the threadpool; `/ready` ping exceeds k8s default probe timeout | **PARTLY DONE** — N.1 (`dffeb80`) removed the four hardcoded `2.0`s: `DODEAL_REDIS_CONNECT_TIMEOUT_SECONDS` `0.25`, `DODEAL_REDIS_SOCKET_TIMEOUT_SECONDS` `1.0`, and a bounded pool on both clients (`DODEAL_REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS` `1.0`; sized from `DODEAL_MAX_INFLIGHT` since N.3b, so 36 by default). The `/ready` pings run gathered, under the 1.0s socket timeout. The breaker's Redis half is built (N.3, `3199e3d`; hardened in N.3b). The pool's refusal, a dead host and the breaker's reaction to it have run against a real server (N.4, `44e9071`). **Unmeasured:** a host that accepts the connection and then stalls costs `/ready` one full socket timeout. That is 1.0s, the same as k8s's default probe timeout rather than under it. | N.1 (`dffeb80`) + N.3 (`3199e3d`) + N.4 |
| H4 | Gate 2 case-sensitive on Host; base domain unchecked | DONE | fix 3 |
| H5 | No JWT clock-skew leeway; skew indistinguishable from forgery | DONE | fix 4 |
| H6 | Tenant claim unvalidated and interpolated into the outbound URL | DONE | fix 3 |
| H7 | RS256 impossible without `cryptography` | DONE | fix 4 |
| M1 | New `AsyncClient` per call; hardcoded transport timeout | PLANNED | step 4 |
| M2 | All-or-nothing page validation; `bookedAmount: float` for an unusable field | PLANNED | step 4 |
| M3 | `get_leads()` silently returns page 1 only | PLANNED | step 4 (rename `get_leads_page`) |
| M4 | Lua sets EXPIRE only on create; pre-existing key without TTL never expires | PLANNED — the behaviour is **pinned by executing tests** in two lanes, and the fix has to change all three deliberately: `tests/unit/test_cost_lua.py` (request script) and, on a real server since N.4, `tests/redis_real/test_scripts_real.py` for the request script **and the token script**, which has the same edge | step 3 |
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
| Hermetic default run — no test opens a socket to any Redis, with or without one listening: the root conftest injects the shared fakes, points the service URLs at a closed port and counts real pool builds. The `redis_real` lane is the exception, and the only one that needs a live server | Piece 103 | DONE |

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
| Q22 | Is a CRM note id unique within a tenant and never reused, including after a delete? (ASSUMPTIONS §4.8) | Backend | UNASKED | the attempt cap, the resubmission reference |

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
| 3 | `enforce_token_cost` beside `enforce_cost` (own namespace, own limits, the shared `cost_window_seconds`); fail-open pre-flight read; the key test (token charge leaves request counters untouched and vice versa). **N.1 DONE (`dffeb80`):** H3 timeouts to `Settings` + bounded pools, `/ready` reporting db2, M5 `fakeredis[lua]`. **N.2 DONE (`77df41d`):** `enforce_token_cost` and its second Lua script, the real `token_preflight` (stub and marker retired), `TokenBudgetExceeded` 429, the warning ratio, `test_token_and_request_counters_never_touch`. **N.3 DONE (`3199e3d`):** the H3 breaker's Redis half. **N.4 DONE (`44e9071`):** the real-Redis lane. **Still owed, unscheduled:** the M4 TTL fix, policy-per-caller for fail-closed workers | H3 timeouts (DONE — N.1); H3 breaker (Redis half DONE — N.3; provider half is A9); M4 TTL fix; M5 `fakeredis[lua]` (DONE — N.1); M7 client type (DONE — D2 part 1); policy-per-caller for fail-closed workers | D2 accepted (`9dca80d`) |
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
