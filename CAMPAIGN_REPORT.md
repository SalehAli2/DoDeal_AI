# Campaign report — Unit A Project 1

**Started:** 1 Sep 2026
**Branch:** `scaffold/core-governance-homes`
**Base head:** `36a0210` — refactor(workers): delete Celery, add the arq skeleton (D2, part 3 of 3)

Resume mechanism: a session may stop at any phase boundary. The next session runs Phase 0,
reads this file, resumes at the first phase not `DONE`, and never redoes a `DONE` phase.

**Sha convention:** a phase's own commit sha cannot be written into the report block that the
commit contains, and amending is forbidden. Each phase therefore backfills the PREVIOUS phase's
sha as its first edit. A block reading `DONE <sha>` means the phase is committed and the next
phase has not started.

**Report format, every phase:**

```
## Phase X — <title>   STATUS: DONE <sha>
**What changed:** one line per file.
**Decisions taken here:** each with the alternative and its cost.
**Tree disagreements:** anything that contradicted §2 or the documents, and what you did.
**Tests:** N added; suite N total, X.XX %; floors met (new floors listed).
**For the lead:** one line each; empty is valid.
```

---

## Phase 0 — confirmation

Every session appends a dated block here. No code changes are made in Phase 0.

### 1 Sep 2026 — session 1 (campaign start)

**Tree state.** Branch `scaffold/core-governance-homes`; head `36a0210`; `git status` clean
(`nothing to commit, working tree clean`). No `CAMPAIGN_REPORT.md` existed — this is a fresh
campaign, resuming at **Phase A**.

**Stopping chain — green, matches the prompt's stated baseline exactly:**

```
221 passed, 7 deselected in 4.80s
Required test coverage of 92.0% reached. Total coverage: 98.42%
ruff check .          All checks passed!
ruff format --check . 88 files already formatted
mypy                  Success: no issues found in 47 source files
check_coverage_floors All 6 coverage floors met.
```

**§2 facts confirmed (the six Phase A depends on):**

| Fact | Confirmed | Where |
| --- | --- | --- |
| Floors are a `_FLOORS: dict[str, float]` of glob → percent; 6 entries today | yes | `scripts/check_coverage_floors.py:35-42` |
| mypy scope is `files = ["src", "tests/helpers"]` — `tests/unit` is **not** type-checked | yes | `pyproject.toml:51` |
| `units/structured_intelligence/` holds only an empty `__init__.py` | yes | `find src/dodeal_ai/units -type f` |
| `build_prompt(template_name: str, caller_data: str) -> AssembledPrompt` | yes | `core/prompting.py:102` |
| `validate_output[M: BaseModel](schema, raw, *, label) -> M` — no context channel | yes | `core/validation.py:47` |
| `LeadNote(id: int, note: str, author: str \| None, author_id: int, createdAt: str)` | yes | `schemas/lead.py:99-103` |

Also re-confirmed: `asyncio_mode = "auto"`; the only pytest marker is `integration`;
`prompts/structured_intelligence/` exists holding only `.gitkeep` (so a new `<task>_v<N>.txt`
lands in a directory that already ships).

**No tree disagreements with §2 found in Phase 0.**

### 3 Sep 2026 — session 2 (pre-D housekeeping)

Not a campaign phase. One commit clearing register items 6 and 8 and the line-ending debt; it
touches nothing Phases D–J own and nothing on the do-not-touch list. Phase D starts from it.

**Tree state.** Branch `scaffold/core-governance-homes`, 3 commits ahead of `origin`; head
`317619f`, `git status` clean apart from an untracked `AIService.zip` this session leaves alone.

```
317619f unit-a(C): judgement routes, TenantScope, DodealError, SEAM[STEP3]    <- Phase C
42cd11c unit-a(B): db2 operational client — idempotency, rate limit, attempts <- Phase B
df4689b unit-a(A): unit schemas and TenantConfig seam                         <- Phase A
36a0210 refactor(workers): delete Celery, add the arq skeleton (D2, part 3 of 3)
87cab6f refactor(api): gates, probe and health to async def (D2, part 2 of 3)
```

**Phase C's sha is `317619f`** — the head. Phase D backfills it into the Phase C heading, which
still reads `STATUS: DONE <sha>`. No archaeology needed.

**Stopping chain before any change — green, matching the stated baseline:**

```
397 passed, 7 deselected in 5.79s
Required test coverage of 92.0% reached. Total coverage: 98.99%
ruff check .          All checks passed!
ruff format --check . 103 files already formatted
mypy                  Success: no issues found in 54 source files
```

**Line endings.** 21 tracked files were `w/crlf`; **zero `i/crlf` anywhere in the repo** (checked
across every tracked file, not only the six path globs), so `.gitattributes` (`* text=auto eol=lf`)
had already normalised each blob and step 2d's `--renormalize` branch does not apply. The 21:

`docker-compose.yml` · `api/routes/_probe.py` · `core/audit/logger.py` · `core/auth/claims.py` ·
`core/auth/dependencies.py` · `core/context.py` · `core/cost/limiter.py` · `core/errors.py` ·
`core/prompting.py` · `core/redis.py` · `main.py` · `prompts/unit_a_v1.txt` ·
`tools/httpx_transport.py` · `tests/helpers/fake_llm.py` · `tests/helpers/test_fake_llm.py` ·
`tests/security/conftest.py` · `tests/security/test_errors.py` ·
`tests/unit/test_assembled_prompt.py` · `tests/unit/test_health.py` ·
`tests/unit/test_llm_seam.py` · `tests/unit/test_redis.py`

**Compose.** `api: environment:` carried `DODEAL_REDIS_QUEUE_URL` (db0) and `DODEAL_REDIS_COST_URL`
(db1) and no operational URL — confirmed; that omission is item 6.

**The bypass warning.** `core/cost/limiter.py:123` read
`_logger.warning("cost_cap_bypassed reason=cost_store_unavailable tenant=%s", tenant)` — the tenant
interpolated into the message string. Target shape confirmed at
`units/structured_intelligence/state.py:108-114`: `_bypass(code, tenant, request_id)` emitting
`extra={"reason_code": ..., "tenant": ..., "request_id": ...}`.

Every assertion on the event, before the change:

| Where | What it asserted | Effect of 2c |
| --- | --- | --- |
| `test_cost.py:86` (`test_redis_down_fails_open_with_warning`) | `"cost_cap_bypassed" in r.getMessage()` — substring | passes unchanged; the message is still exactly that |
| `test_cost.py` (`test_atomic_failure_leaves_neither_counter_touched`) | captures at WARNING but asserts on counters, not on text | untouched |
| `test_logging_config.py:84-94` | emitted the **old formatted string by hand**, then asserted level, logger, and `"cost_cap_bypassed" in record["message"]` | rewritten to the new call shape — it was testing a string the code no longer emits |
| `docs/runbooks/secret-rotation.md:178` | names `cost_cap_bypassed` as the alert to look for | still accurate; the event name did not change, so the runbook is not edited |
| `state.py:23` (docstring) | "the same shape as `core/cost/limiter.py`'s `cost_cap_bypassed`" | **not** stale — aspirational before, literally true now, so it is left alone |

`tests/security/test_log_safety.py` declares sentinels as a module-level constant (`SENTINEL`,
shaped like real note content) plus a `log_capture` fixture attaching the **real** `JsonFormatter`
to the whole `dodeal_ai` tree, and a `_lines()` helper parsing the emitted JSON — so a new sentinel
drives the real code path and asserts on the parsed line.

**`.env.example` is still unreadable.** `Get-Content .env.example` →
`blocked. For security, Claude Code may only access files in the allowed working directories`. The
same denial Phase B hit. Step 2b skipped.

**What changed:**

- `docker-compose.yml` — `DODEAL_REDIS_OPERATIONAL_URL: redis://redis:6379/2` under
  `api: environment:`, directly after `DODEAL_REDIS_COST_URL`. Item 6.
- `src/dodeal_ai/core/cost/limiter.py` — the fail-open warning now emits `cost_cap_bypassed` with
  `extra={"reason_code": "cost_store_unavailable", "tenant": tenant}` instead of interpolating the
  tenant into the message; the comment above it records why there is no `request_id`. Item 8.
- `tests/unit/test_cost.py` — one test added: the `LogRecord` carries `reason_code` and `tenant` as
  attributes, and `getMessage()` is exactly `cost_cap_bypassed`.
- `tests/unit/test_logging_config.py` — the hand-rolled old string replaced by the real call shape;
  now asserts `message == "cost_cap_bypassed"` plus top-level `reason_code` and `tenant` in the JSON.
- `tests/security/test_log_safety.py` — one sentinel added: driving a real Redis outage through
  `enforce_cost`, the tenant label appears in the `tenant` field and **never** inside `message`.
- 21 files rewritten CRLF → LF in the working tree. **Zero content change** — see below.
- `CAMPAIGN_REPORT.md` — this block.

**Decisions taken here:**

- Step 2d was run **first**, before any content edit, so its "no change" evidence is about endings
  alone and not entangled with the other three changes. The alternative — prompt order — costs
  nothing but makes the `git diff` proof ambiguous.
- `docker-compose.yml` has **never** been newline-terminated (`HEAD:docker-compose.yml` ends
  `...unless-stopped`, no trailing byte). Left as it is: adding one would be a second change to a
  file the prompt says nothing else changes. Cost: the file is still not newline-terminated.
- The new sentinel uses `tenant-b`, per the fixture-naming rule, rather than a bespoke
  `TENANT-SENTINEL` string. `tenant-b` cannot occur inside the fixed event name, so the absence
  assertion is just as sharp.

**Tree disagreements:**

- **`git status` does not go quiet after the LF rewrite, contrary to step 2d.** All 21 files showed
  ` M` while `git diff`, `git diff HEAD` and `git diff --cached` were all **empty**, and per file
  the worktree hash equalled the index blob equalled the HEAD blob (`git hash-object` vs
  `git ls-files -s` vs `git rev-parse HEAD:<path>`). The cause is `core.autocrlf=true` inherited
  from the **system** gitconfig (`C:/Program Files/Git/etc/gitconfig` — Git-for-Windows' installer
  default) fighting `.gitattributes`' `eol=lf`: the index stat cache goes permanently dirty for a
  file whose working copy is LF while autocrlf says it "should" be CRLF, and
  `git update-index --really-refresh` returns rc=1 `needs update` for exactly those 21.
  `git add -- <path>` settles each entry and stages **nothing** (`git diff --cached` stayed empty).
  So step 2d held in substance — the rewrite contributed zero bytes to the commit — and only the
  porcelain marker misbehaved. This is the failure `.gitattributes`' own header comment warns
  about, seen from the other side.
- Not a disagreement, but worth recording: the campaign's stopping chain is four blocks, while
  session 1's block records a fifth, `scripts/check_coverage_floors.py`. It was run: all floors met.

**Tests:** 2 added; suite **399 total**, **98.99 %** (floor 92); all 10 per-file floors met,
`limiter.py` still at 100 %. No floor added or changed. mypy: 54 source files, clean.

**For the lead:**

- **`.env.example` line not added; path denied; the lead adds it by hand:**
  `DODEAL_REDIS_OPERATIONAL_URL=redis://localhost:6379/2`, directly after `DODEAL_REDIS_COST_URL`
  to keep the file in `Settings` source order. This is the second session to hit the denial — the
  request has been open since Phase B.
- **Phase C's sha, for Phase D to backfill: `317619f`.**
- `core.autocrlf=true` is set in this machine's system gitconfig. It cannot corrupt a blob here
  (`.gitattributes` normalises on commit), but it makes `git status` report phantom modifications
  after any LF rewrite. Worth `git config --global core.autocrlf input` on dev machines; this
  session did not change git config.
- `scripts/check_coverage_floors.py` prints 18 rows but reports "All 10 coverage floors met" —
  `config.py`, `state.py` and `pipeline.py` each match two globs and are checked twice against
  different floors. Harmless today; confusing when Phase H raises `pipeline.py`'s floor, because
  the 90 row and the 95 row will both still apply.
- `AIService.zip` is untracked at the repo root and was left alone. If it is not deliberate it
  wants a `.gitignore` line or a delete.

---

### 3 Sep 2026 — session 3 (Phases D, E, F)

**Scope of this session:** Phases D, E and F. It stops at the boundary before G and does not
reach H.

**Spec provenance.** Read from `docs/campaign/UNIT_A_PROJECT1_CAMPAIGN_PROMPT.md` in the
repository (38 674 bytes), not a pasted copy. `### Phase J — the ledger commit` is at line 479
and `## 7. Never, in any phase` at line 521, so the file is complete. First line of every phase
section D–J, quoted:

| Line | First line of the section |
| --- | --- |
| 334 | `### Phase D — classification` |
| 354 | `### Phase E — vague detection` |
| 368 | `### Phase F — scoring` |
| 398 | ``### Phase G — reprompt once via `AssembledPrompt.tail` `` |
| 414 | `### Phase H — decide, clarification loop, rate limit. **The lead reviews this phase's report hardest.**` |
| 457 | `### Phase I — prompt hardening, adversarial suite, OWASP checkpoint, eval marker` |
| 479 | `### Phase J — the ledger commit` |

**Resume point.** Phases A, B, C are `DONE`. **Phase C's sha `317619f` backfilled into its
heading** as this session's first edit, per the sha convention. The first phase not `DONE` is
**D**.

**Tree state.** Branch `scaffold/core-governance-homes`, head `28ee8bf` (session 2's
housekeeping commit), 4 commits ahead of `origin`. `git status --porcelain`:

```
 M .env.example        <- the lead's edit, unstaged; see "For the lead" below
?? AIService.zip       <- untracked, left alone (noted by session 2)
?? docs/campaign/      <- the spec itself, untracked; the lead's to commit, not a session's
```

**Stopping chain before any change — green:**

```
399 passed, 7 deselected in 5.57s
Required test coverage of 92.0% reached. Total coverage: 98.99%
ruff check .          All checks passed!
ruff format --check . 104 files already formatted
mypy                  Success: no issues found in 54 source files
check_coverage_floors All 10 coverage floors met.
```

#### The five verifications §4 demands

**(a) Fail-open / fail-closed, five lines.**

1. Auth (Gate 1) and tenancy (Gate 2) — **CLOSED**: 401 / 403, `audit(decision="deny")`.
2. Request cost (Gate 4), db1 unreachable — **OPEN**: the request proceeds, `cost_cap_bypassed`
   at WARNING.
3. Idempotency, db2 unreachable — **CLOSED**: 503 `idempotency_unavailable`; nothing is spent.
4. Rate limit and attempt counter, db2 unreachable — **OPEN**: proceed, `rate_limit_bypassed` /
   `attempt_counter_bypassed` at WARNING once per call.
5. Model call — **enumerated**: `LLMProviderError` → 503 `model_unavailable`, `retry=False`,
   never retried; the idempotency key is released so the caller can try again.

**(b) The three reachable endpoints, and nothing is `[V]`.** `LeadsClient` exposes exactly
`get_leads` (`GET /leads`), `get_lead` (`GET /leads/{id}`) and `get_lead_notes`
(`GET /leads/{id}/notes`) — `tools/leads.py:95,104,111` — each taking a `TenantScope`, keyed by
`DD-API-KEY`, wrapped by the watchdog. No fourth method and no query parameter exists.
`ASSUMPTIONS.md:23` still reads **"Nothing is tagged `[V]`"**: no successful live call to the
real backend has ever been made, so every shape in `schemas/lead.py` is assumed or documented,
never verified. This campaign adds no `[V]` either — it runs against `FakeLeadsClient` and
`FakeLLM` only.

**(c) `AssembledPrompt.tail`.** `core/prompting.py:58-62` — "a trusted trailing instruction
rendered AFTER the data section. Reserved for the Step 10 reprompt … Always sourced from a
versioned file, never from a caller. Empty today; `build_prompt()` does not populate it yet."
That is **Phase G's** slot and no earlier phase writes it: `.text` renders
`stable + "\n\n" + variable`, and appends `+ "\n\n" + tail` only when the tail is non-empty, so
the byte-identity of the first prompt is what makes "only `.tail` differs" provable.

**(d) The five provisional answers, and what `SEAM[STEP3]` blocks.**

- **Q1** — the CRM forwards the end user's JWT; the judgement routes run Gate 1 → 2 → 4 via one
  `Depends(gate4_cost)`. Correction: the principal source swaps behind D1's seam; subjects
  become *asserted*; no route contract moves.
- **Q6** — the notes feed may carry machine timeline entries; they classify as `system_event`
  and suppress `not_scorable`, never scored. Correction: if the feed is notes-only the member
  stays in the vocabulary and is simply never emitted. Nothing to rescore.
- **Q7** — the rate limit keys on `scope.subject` (who is *asking*), never `note.author_id`
  (who *wrote* it), and increments only when a prompt is actually sent. Correction: if Product
  wants per-author limits the key changes; counters are windowed, so no history is invalidated.
- **Q8** — the target note is on page one of the lead's notes; not found there → 404
  `note_not_found`. Nothing branches on it. Correction: paging arrives with query parameters in
  `tools/leads.py` at step 4.
- **Q13** — no lead field is confirmed to carry the business line, so
  `deal_specifics_applicable=False` and `deal_specifics` is suppressed for every type; the
  denominator falls 100 → 80. Correction: name the field, add the per-line checklist, flip the
  flag, bump `config_version`, **never rescore history**.
- **`SEAM[STEP3]`** blocks the **token-budget pre-flight**.
  `core/cost/limiter.py::token_preflight` is a loud no-op logging `token_preflight_bypassed`
  once per process. Until step 3 lands there is no read of a token budget before a model call,
  and — the reason it is a seam and not a TODO — **no real provider may be wired before it**:
  `get_llm_client()` still raises, and `FakeLLM` is the only model in this campaign. Phases D, E
  and F all call the model *behind* this seam and none of them touch it.

**(e) Why the band is derived and never accepted.** A band is a business judgement about a
salesperson's work, and the only thing that makes it defensible is that it is reproducible from
marks + `TenantConfig` + the four version stamps. If a band could arrive from a model, from a
caller, or from a stored value, an identical note could carry two different bands with no
recorded reason, and the tenant could not change a weight without silently invalidating history.
So the model supplies marks only; `total`, `denominator`, `band` and `decision` are computed in
code. It is enforced structurally, not by convention: no model-output schema has a `band` or a
`total` field (`schemas.py:23-28`), and `TenantConfig.band_for()` is the one function that
produces a `Band`.

#### Inspection — the seams Phases D, E and F sit on

| Path | What it is, in one line |
| --- | --- |
| `core/prompting.py:102` | `build_prompt(template_name, caller_data) -> AssembledPrompt`; loads `prompts/<name>` (`PromptError` if missing, `.strip()`ed), wraps caller data in the BEGIN/END markers, and rewrites either marker found inside it to `[filtered-delimiter]`. **No `tail` parameter yet — that is Phase G.** |
| `core/llm/client.py:113` | `LLMClient` Protocol, one method: `async complete(prompt, *, max_output_tokens=None) -> LLMResponse`. |
| `core/llm/client.py:82` | `LLMProviderError(reason, *, transient)`; `str()` is `llm_provider_error:<reason>` and carries no provider text. |
| `core/llm/client.py:36` | `FinishReason.STOP / MAX_TOKENS / OTHER` — truncation is signalled as `MAX_TOKENS`; `LLMResponse.text` is `field(repr=False)`. |
| `core/validation.py:47` | `validate_output[M](schema, raw: object, *, label) -> M` — **`label` is keyword-only**, and `raw` is an *object*, not a string: JSON decoding is the caller's job. Raises `OutputValidationError(label, errors)` carrying only `(dotted_loc, pydantic_type)` pairs, unchained (`from None`), after logging `output_validation_failed`. |
| `core/resilience.py:48` | `call_with_watchdog(op, *, label, timeout, retry)`; wraps **any** exception into `ExternalCallError(label, cause)` with `.cause` preserved. Validation must therefore run *outside* the wrapped operation, or an `OutputValidationError` would come back as an `ExternalCallError`. |
| `core/config.py:105-113` | `llm_model: str = ""`, `llm_timeout_seconds: float = 60.0`, `llm_max_output_tokens: int = 1024`, `prompts_dir: Path or None = None`. |
| `core/errors.py:32-103` | `DodealError(reason_code, http_status)` + eight subclasses; `ModelUnavailableError` (503) and `MalformedOutputError` (503) already exist, so far unraised. |
| `units/.../pipeline.py:159` | `_fetch_note(scope, request, deps) -> tuple[int, LeadNote]` — returns `note.author_id` and the note, and **discards the `Lead`**. Phase D needs the lead for the classifier's context section, so this signature changes. |
| `units/.../config.py:99-115` | `allowed_missing_by_type` **already exists** (Phase A built it): `no_contact` → `{what_happened, next_step_with_date}`, every other type → all three. Phase E's "add a per-type allowed set in `config.py`" is therefore already satisfied. |
| `units/.../schemas.py:200-267` | `ClassificationOutput`, `VagueOutput` (biconditional validator), `ScoreOutput` — all `extra="forbid"`, none carrying a band or a total. |
| `tests/helpers/fake_llm.py` | **Already scripts an ordered sequence and counts calls**: `FakeLLM(*script)`, `.script(*more)`, `.calls`, `.call_count`, `.prompts`; an exhausted script raises `FakeLLMExhausted`. Phase D therefore needs **no extension** to it. |
| `tests/helpers/fake_leads.py` | `FakeLeadsClient` — dict-backed, records `RecordedFetch(method, tenant, lead_id)`, `raise_on` maps a method name to the exception it raises. Helpers `lead(id, **overrides)` and `note(id, text, ...)`. |
| `tests/helpers/fake_operational_redis.py` | `FakeOperationalRedis(raise_on={...})` implementing `set/get/incr/ttl/expire/delete`; injected with `monkeypatch.setattr(state, "get_operational_client", ...)`. |
| `tests/unit/test_judgement_routes.py:79-107` | The route fixture: gate-chain wiring plus `dependency_overrides` for `get_leads_client`, `get_llm_client` and `get_verifier`, the fake cost redis, the fake db2, and the once-per-process `_TOKEN_PREFLIGHT_LOGGED` reset. |
| `scripts/check_coverage_floors.py:46-62` | `_FLOORS: dict[glob, percent]`; `units/structured_intelligence/**` is already at 95, so a new file in the unit inherits 95 and only a *higher* floor needs its own entry. |
| `pyproject.toml:20-21` | `[tool.hatch.build.targets.wheel] packages = ["src/dodeal_ai"]` — hatchling ships every file under the package, so a new `prompts/structured_intelligence/*.txt` is in the wheel without a `pyproject.toml` change. |
| `pyproject.toml:23-29` | `asyncio_mode = "auto"`; the only marker is `integration`; the default run is `-m "not integration"`. mypy scope is `src` + `tests/helpers`. |

**Tree disagreements found in Phase 0 (both matter to a later phase, neither blocks D–F):**

- **There is no local fake CRM app, no 127-note corpus and no injection fixtures.** §4 asks
  Phase 0 to record "where it lives (fixture, script or app), how tests point `LeadsClient` at
  it, how the 127-note corpus and the four injection fixtures are loaded, the 'switchable
  unknown-flags'". None of it exists: the only fake CRM is `tests/helpers/fake_leads.py`, a
  dict-backed `FakeLeadsClient` injected through
  `app.dependency_overrides[get_leads_client]`, and searching `tests/` for `*.json`, `*.jsonl`
  and `*.csv` returns nothing. **Following the tree**, as the campaign's own rule requires.
  Phases D–F need only scripted notes and are unaffected. **Phase I is affected**: its
  adversarial suite is specified against "the four corpus injection fixtures" and its eval
  skeleton against "the whole fake corpus". Whoever runs I must either build the corpus first or
  scale the phase to constructed fixtures — flagged now rather than at the start of I.
- **`validate_output`'s `label` is keyword-only** (`validate_output(schema, raw, *, label=...)`),
  where §4 writes it positionally. Cosmetic; call sites use the keyword.

### 3 Sep 2026 — session 4 (hotfix: lazy `WorkerSettings.redis_settings`)

**Scope of this session:** one hotfix commit. No phase advances. **Phase G is not started.**
`workers/runner.py` is exempt from the do-not-touch list for this commit only, by the lead's
instruction; every other rule in §0 and §7 still applies.

**Sha convention.** **Phase F's sha `2f2dbfb` backfilled into its heading** as this session's
first edit.

**Tree state.** Branch `scaffold/core-governance-homes`, head `2f2dbfb` (Phase F).
`git status --porcelain`:

```
 M .env.example        <- the lead's edit, still unstaged; untouched
?? AIService.zip       <- untracked, left alone
?? docs/campaign/      <- the spec itself, untracked; the lead's to commit
```

**What is broken.** CI is red on `tests/unit/test_worker_settings.py` — not at assertion time,
at **collection**. `src/dodeal_ai/workers/runner.py` called `redis_settings()` in the class body
of `WorkerSettings`, so merely importing the module built `Settings`. `jwt_signing_key` has no
default (fail-closed, `core/config.py`), CI has neither the env var nor a `.env`, and the root
`conftest.py` autouse fixture that supplies the key runs **after** collection. The import
therefore raised `ConfigError` before any fixture could exist, taking the whole file with it.

**Why it never showed locally, and why the wheel check passes.** Two separate masks:

- A developer machine has a `.env` at the repo root, and pydantic-settings reads it
  (`env_file=".env"`), so the eager read succeeded.
- `scripts/verify_wheel.py:115` passes `DODEAL_JWT_SIGNING_KEY: "verify-wheel-dummy-key"` in the
  subprocess env, so the wheel import check supplied the key itself and never exercised the bug.

**Pre-existing, not a campaign regression.** The line dates from `36a0210` (the arq skeleton,
the campaign's base head). No phase A–F touched `workers/`.

**Reproduction, recorded.** `.env` is permission-blocked in this session, so it was moved with
`mv .env .env.off`, not renamed in PowerShell; the env var was cleared in the same shell. Moved
back immediately after.

```
$ Remove-Item Env:DODEAL_JWT_SIGNING_KEY -ErrorAction SilentlyContinue
$ uv run pytest tests/unit/test_worker_settings.py --no-cov
collected 0 items / 1 error
ERROR collecting tests/unit/test_worker_settings.py
  tests/unit/test_worker_settings.py:11: in <module>
      from dodeal_ai.workers import runner
  src/dodeal_ai/workers/runner.py:44: in WorkerSettings
      redis_settings = redis_settings()
  src/dodeal_ai/core/config.py:151: in _build_settings
      raise ConfigError(
  E   dodeal_ai.core.config.ConfigError: Missing or invalid required configuration; refusing to start.
!!!!!!! Interrupted: 1 error during collection !!!!!!!
```

**The fix.** A `_LazyRedisSettings` descriptor whose `__get__` returns `redis_settings()`, so the
read happens when arq reads the attribute to start the worker, not when the class is created.
`redis_settings()` stays the module function; `WorkerSettings.functions` is unchanged; the
step-14 TRIGGER comment is unchanged. **Register item 7, pulled forward from step 14 — CI.**

**Decision taken here: descriptor, not `classmethod`+`property` or `__getattr__`.** A property
does not fire on class attribute access, and arq reads `WorkerSettings.redis_settings` off the
class, not an instance — so a property would have handed arq the property object itself.
Chaining `classmethod` with `property` was removed in 3.13 and is deprecated in 3.12, so it
would have been a known-dated construct. A module-level `__getattr__` cannot intercept a class
attribute at all. The descriptor is eight lines, types cleanly under the strict mypy config, and
re-reads current settings on every access — the cost is one `lru_cache` hit per access, which is
what `get_settings()` is for.

**Tests:** 1 added (`test_import_reads_no_settings`), 1 strengthened.

- `test_import_reads_no_settings` runs `[sys.executable, "-c", ...]` with every `DODEAL_` var
  stripped, `cwd` the repo root. It imports the module **and asserts
  `get_settings.cache_info().currsize == 0`**. The cache assertion is load-bearing: `cwd` must be
  the repo root for the package to resolve, a repo root has a `.env`, and a `.env` satisfies the
  eager read — so a returncode-only check would have passed on this machine against the old code
  while still failing in CI. Verified negatively: restored the eager `runner.py`, ran the new
  test, got `AssertionError: import built Settings` (not `ConfigError` — the `.env` mask,
  demonstrated).
- `test_worker_settings_carry_redis_settings` now takes the `queue_url` fixture and asserts
  `runner.WorkerSettings.redis_settings.host == "queue.example"`. The old `is not None` passed
  against a value captured at import; this proves the descriptor reads the settings **current at
  access time**, since the fixture set that URL long after the module was imported.

**Suite:** 601 passed, 7 deselected, total coverage **99.12 %** (floor 92). All 11 per-file
coverage floors met; no floor added or changed. `src/dodeal_ai/workers/runner.py` 13 statements,
**100 %**. Chain green in order: `pytest`, `ruff check .`, `ruff format --check .`, `mypy`
(58 source files, clean under the strict config).

**Tree disagreements:** none. The change is confined to the one file the lead exempted, plus its
test.

**For the lead:**

- **"Appendix B #11" does not exist in the tree.** Nothing in `docs/`, `CAMPAIGN_REPORT.md` or
  `docs/campaign/UNIT_A_PROJECT1_CAMPAIGN_PROMPT.md` contains the string "Appendix B" — the
  campaign prompt has no appendices at all. It is cited in the commit body as instructed, but it
  is a pointer into a document this repository does not hold, so no session can check the commit
  against it. Same for the register itself: "register item 7" is referenced by number in the
  prompt and by number in several places in the spec, but the register is not in the tree.
- **The `.env` mask is general, not specific to `workers/`.** Any module that reads settings at
  import time will pass locally and fail CI at collection in exactly this way, and the wheel
  check will not catch it because it injects a dummy key. The new test only guards
  `workers/runner.py`. A one-line grep test over `src/` for module-scope `get_settings()` calls
  would guard the class of bug rather than this instance — not done here, since this commit is
  scoped to the hotfix.

### 5 Sep 2026 — session 5 (Phase G)

**Scope of this session:** Phase G only. It stops at the boundary before H and does not start it.

**Spec provenance.** Read from `docs/campaign/UNIT_A_PROJECT1_CAMPAIGN_PROMPT.md` in the repository
(38 674 bytes — unchanged since session 3), not a pasted copy. `### Phase J — the ledger commit` is at
line 479 and `## 7. Never, in any phase` at line 521, so the file is complete. First line of every phase
section D–J, quoted:

| Line | First line of the section |
| --- | --- |
| 334 | `### Phase D — classification` |
| 354 | `### Phase E — vague detection` |
| 368 | `### Phase F — scoring` |
| 398 | ``### Phase G — reprompt once via `AssembledPrompt.tail` `` |
| 414 | `### Phase H — decide, clarification loop, rate limit. **The lead reviews this phase's report hardest.**` |
| 457 | `### Phase I — prompt hardening, adversarial suite, OWASP checkpoint, eval marker` |
| 479 | `### Phase J — the ledger commit` |

**Resume point.** Phases A–F are `DONE`. **The first phase not `DONE` is G.** No sha to backfill: session 4
already backfilled Phase F's `2f2dbfb` into its heading, and the current head `d0fa1e1` is that session's
hotfix, not a phase commit.

**Tree state.** Branch `scaffold/core-governance-homes`, head `d0fa1e1`, **0 commits ahead of
`origin/scaffold/core-governance-homes`** — the lead has pushed A–F and the hotfix.
`git status --porcelain`:

```
 M .env.example        <- the lead's edit, still unstaged; untouched again
?? AIService.zip       <- untracked, left alone
?? docs/campaign/      <- the spec itself, untracked; the lead's to commit
```

`?? docs/audit/2026-09-05-sweep.md` appeared **during** this session, after the work started. It is the
lead's, untracked, and was not touched, staged or read into the phase. It does change what the fourth
stopping-chain block reports — see the closing chain below and "For the lead".

**Stopping chain before any change — green:**

```
601 passed, 7 deselected in 9.38s
Required test coverage of 92.0% reached. Total coverage: 99.12%
ruff check .          All checks passed!
ruff format --check . 111 files already formatted
mypy                  Success: no issues found in 58 source files
check_coverage_floors All 11 coverage floors met.
```

#### The five verifications §4 demands

**(a) Fail-open / fail-closed, five lines.** Unchanged from session 3 and re-checked against the code, not
the report: (1) auth and tenancy **CLOSED**, 401/403 with `audit(decision="deny")`; (2) request cost (Gate 4)
with db1 down **OPEN**, `cost_cap_bypassed` at WARNING; (3) idempotency with db2 down **CLOSED**, 503
`idempotency_unavailable` — `state.reserve_idempotency` raises `IdempotencyUnavailableError`, caught at
`pipeline.py:303`; (4) rate limit and attempt counter with db2 down **OPEN**, `rate_limit_bypassed` /
`attempt_counter_bypassed` once per call; (5) model failure **enumerated** — `LLMProviderError` → 503
`model_unavailable`, `retry=False`, never retried, key released. **Phase G adds no sixth policy.** The
reprompt is not a retry and is not a fail-open: a malformed answer is still an enumerated failure, it just
takes two calls to reach it instead of one.

**(b) The three reachable endpoints, and nothing is `[V]`.** `LeadsClient` still exposes exactly `get_leads`
(`tools/leads.py:95`), `get_lead` (`:104`) and `get_lead_notes` (`:111`), each taking a `TenantScope`. No
fourth method, no query parameter. `ASSUMPTIONS.md:23` still reads "Nothing is tagged `[V]`", and this phase
adds none — it touches no backend call at all.

**(c) `AssembledPrompt.tail`.** Reserved, until this phase, for exactly the Step 10 reprompt: "a trusted
trailing instruction rendered AFTER the data section … always sourced from a versioned file, never from a
caller." `.text` renders `stable + "\n\n" + variable` and appends `+ "\n\n" + tail` only when the tail is
non-empty, which is what makes "the first prompt is byte-identical" provable rather than asserted.
**This is the phase that fills it**, through the one function that can: `core/prompting.py:117 with_tail`.

**(d) The five provisional answers, and what `SEAM[STEP3]` blocks.** Unchanged and un-reopened: Q1 (the CRM
forwards the end user's JWT; correction behind D1's seam), Q6 (`system_event` suppresses `not_scorable`;
nothing to rescore), Q7 (rate limit keys on `scope.subject`, never `note.author_id`), Q8 (the note is on page
one; paging is step 4), Q13 (`deal_specifics_applicable=False`, denominator 100 → 80; never rescore history).
`SEAM[STEP3]` blocks the **token-budget pre-flight** — `core/cost/limiter.py::token_preflight` is a loud
no-op logging `token_preflight_bypassed` once per process, and until step 3 lands **no real provider may be
wired**. Phase G calls the model twice on a bad answer and still touches neither: both calls go through the
same `complete_once`, behind the same seam, against `FakeLLM`.

**(e) Why the band is derived and never accepted.** Unchanged: a band is defensible only because it is
reproducible from marks + `TenantConfig` + four version stamps, so `total`, `denominator`, `band` and
`decision` are computed in code and no model-output schema has a band or a total field. **Phase G is where
that gets its sharpest test**: the reprompt is the one path that sends a second prompt, and if the rejected
answer were fed back, a model could restate a total until something accepted it. It is not fed back — there
is no parameter on `with_tail` that takes a string.

#### Inspection — the seams Phase G sits on

| Path | What it is, in one line |
| --- | --- |
| `core/prompting.py:102` | `build_prompt(template_name, caller_data) -> AssembledPrompt`; loads through `_load_template` (`:92`, `PromptError` if missing, `.strip()`ed) and neutralises either delimiter found in caller data. **No tail parameter** — that gap is this phase's work. |
| `core/prompting.py:48` | `AssembledPrompt` is `frozen=True, slots=True` with `variable` at `repr=False`, so `dataclasses.replace` is available and a tail cannot mutate a prompt a caller still holds. |
| `units/.../llm_call.py:205` | **`call_model`, not `call_validated`** — the tree's name for §5's function, same signature shape (`-> tuple[M, LLMResponse]`), and its own docstring already said "PHASE G inserts the single reprompt … the call sites do not change when it does". |
| `units/.../llm_call.py:157` | `parse_output(response, schema, label, *, check)` — the ONE untrusted-parse boundary. It already took the whole `LLMResponse`, not just `.text`, so `finish_reason` was already in scope for the MAX_TOKENS rule. |
| `core/llm/client.py:36` | `FinishReason.STOP / MAX_TOKENS / OTHER`; the comment on `MAX_TOKENS` already reads "Step 10 treats this differently from a complete-but-malformed response". |
| `core/llm/client.py:116` | `complete(prompt, *, max_output_tokens: int \| None = None)`; `None` = `Settings.llm_max_output_tokens`. The seam has always had the per-call ceiling; nothing was passing one. |
| `core/config.py:111-113` | `llm_max_output_tokens: int = 1024`, and its comment ALREADY reads "Headroom for Arabic, which costs roughly 1.5-3x the tokens of equivalent English. **Tasks override per call.**" Register item 15 is the tree's own outstanding instruction, not a new idea. |
| `tests/helpers/fake_llm.py:78` | `RecordedCall(prompt, max_output_tokens)` — the fake already records the ceiling, so item 15 is testable with no helper change. `truncated(text)` (`:58`) already builds a MAX_TOKENS reply, described as "the likeliest malformed case". |
| `tests/unit/test_scoring.py:431` | The template grep is parametrised over `_PROMPT_DIR.glob("*_v1.txt")`, so a new `reprompt_tail_v1.txt` is checked for `total`/`band`/`poor`/`excellent` automatically. The sibling test pins the filename list and needed updating from eight to nine. |
| `scripts/check_coverage_floors.py:46` | `units/structured_intelligence/**` at 95, so `llm_call.py` inherits 95 and only a higher floor needs its own row. |

**Two rulings from the lead, recorded before the work started:**

- **§2.3's fourth denominator "75" is a document defect.** 60 is correct for `no_contact` in every case, Q13
  resolved or not, and the tree is right. This closes the Phase F disagreement and the "For the lead" item it
  raised; no code, test or config changes — `test_denominator_for_no_contact_with_q13_resolved_is_still_60`
  already asserts 60 and carries the arithmetic in a comment. Phase J's ASSUMPTIONS §3.4 entry should list
  the denominators as **100 / 80 / 60**, not 100/80/60/75.
- **Per-task token ceilings (register item 15) are sized from the longest ARABIC notes, not English.** Arabic
  runs roughly 2–3× the tokens per word; a ceiling set on English turns ordinary Arabic notes into MAX_TOKENS
  truncation, which this phase treats as malformed — so an English-sized ceiling would not degrade an Arabic
  note gracefully, it would spend its one reprompt and then 503 it. Sized accordingly below.

**Tree disagreements found in Phase 0:** none new. The two session 3 recorded still stand — **there is still
no fake CRM app, no 127-note corpus and no injection fixtures** (which is why the ceilings below are sized
from the schema's own cap and a stated tokens-per-word model rather than measured against a corpus; **Phase I
is still the phase that must build the corpus or scale to constructed fixtures**), and `validate_output`'s
`label` is still keyword-only where §4 writes it positionally.

### 6 Sep 2026 — session 6 (Phase H)

**Scope of this session:** Phase H only. It stops at the boundary before I, does not start I, and does
not push. One commit.

**Spec provenance.** Read from `docs/campaign/UNIT_A_PROJECT1_CAMPAIGN_PROMPT.md` in the repository, not
a pasted copy. `### Phase J — the ledger commit` is at line 479 and `## 7. Never, in any phase` at line
521, so the file is complete. First line of every phase section D–J and of §7, quoted:

| Line | First line of the section |
| --- | --- |
| 334 | `### Phase D — classification` |
| 354 | `### Phase E — vague detection` |
| 368 | `### Phase F — scoring` |
| 398 | ``### Phase G — reprompt once via `AssembledPrompt.tail` `` |
| 414 | `### Phase H — decide, clarification loop, rate limit. **The lead reviews this phase's report hardest.**` |
| 457 | `### Phase I — prompt hardening, adversarial suite, OWASP checkpoint, eval marker` |
| 479 | `### Phase J — the ledger commit` |
| 521 | `## 7. Never, in any phase` |

**Resume point.** Phases A–G are `DONE`. **The first phase not `DONE` is H.** Phase G's heading reads
`STATUS: DONE <sha>` — the session that wrote it committed after writing the block and never came back
to fill it in. Backfilled to `103ce02` as this phase's first edit (the lead's ruling §2.1).

**Tree state.** Branch `scaffold/core-governance-homes`, head `103ce020fe8411acec4018f2095a30a846edb396`.

```
$ git log --oneline -15
103ce02 unit-a(G): reprompt once via AssembledPrompt.tail, then 503
d0fa1e1 fix(workers): lazy WorkerSettings.redis_settings — importing reads no settings
2f2dbfb unit-a(F): scoring — marks from the model, arithmetic in code, Q13 suppression
b432af1 unit-a(E): vague detection — per-type prompts, fixed missing-components vocabulary
8874958 unit-a(D): classification against FakeLLM, system_event short-circuit
28ee8bf chore: housekeeping — compose db2 URL, cost bypass extras, line endings
317619f unit-a(C): judgement routes, TenantScope, DodealError, SEAM[STEP3]
42cd11c unit-a(B): db2 operational client — idempotency, rate limit, attempts
df4689b unit-a(A): unit schemas and TenantConfig seam
36a0210 refactor(workers): delete Celery, add the arq skeleton (D2, part 3 of 3)
87cab6f refactor(api): gates, probe and health to async def (D2, part 2 of 3)
811090d test: drop redundant @pytest.mark.asyncio markers
d37945b refactor(redis): migrate the cost path to redis.asyncio (D2, part 1 of 3)
9dca80d docs(status): record D1 and D2 accepted; clear D2 blockers
1d795d0 chore(repo): track .claude/settings.json, completing fix 6b

$ git status --porcelain
 M .env.example
?? AIService.zip
?? docs/audit/
?? docs/campaign/
```

Commit-subject style confirmed: `unit-a(<phase>): <what>`, lowercase, ≤ 72 chars, no trailer. The
modified `.env.example` and the three untracked paths are the lead's and are left alone — this phase
stages by explicit path only.

**Stopping chain before any change** (PowerShell 5, all four blocks read):

```
$ uv run pytest
  634 passed, 7 deselected in 16.28s
  Required test coverage of 92.0% reached. Total coverage: 99.13%

$ uv run ruff check .
  All checks passed!

$ uv run ruff format --check .
  unformatted: File would be reformatted
     --> docs\audit\2026-09-05-sweep.md:190:1
  1 file would be reformatted, 112 files already formatted        (exit 1)

$ uv run ruff format --check src tests scripts        (§0.4's fallback)
  102 files already formatted

$ uv run mypy
  Success: no issues found in 58 source files
```

Block 3 is red **only** on `docs/audit/2026-09-05-sweep.md` — the lead's untracked audit document, whose
fenced Python block at `:190` is missing a blank line. Unchanged from session 5, not touched, not
formatted, not staged. Per §0.4, green over `src tests scripts` is green for this phase.

**The five verifications from spec §4, answered from the code.**

**(a) The fail-open / fail-closed matrix, five lines** — re-verified at `state.py:9-25` and
`core/cost/limiter.py`:

1. Auth (Gate 1) and tenancy (Gate 2) — **CLOSED**, 401 / 403, audited as `decision="deny"`.
2. Request cost (Gate 4) — **OPEN** when db1 is unreachable, `cost_cap_bypassed` at WARNING.
3. Idempotency (db2) — **CLOSED**. `state.reserve_idempotency:157` catches `RedisError` and raises
   `IdempotencyUnavailableError` → 503 `idempotency_unavailable`. There is no safe "probably not a
   duplicate": failing open means paying twice and asking a salesperson the same question twice.
4. Rate limit and attempt counter (db2) — **OPEN**. `read_rate_limit:201` / `read_attempts:236` return
   0 and log `rate_limit_bypassed` / `attempt_counter_bypassed`; the increments swallow and log the same
   codes. A politeness guard must not 503 a judgement that is otherwise fine.
5. Model call — an **enumerated error, never a retry**. `retry=False` at `llm_call.py:140`;
   `ExternalCallError` → `ModelUnavailableError` (503 `model_unavailable`) at `:154`.

Sixth, unchanged: backend reads keep the watchdog's existing retry-once; `ExternalCallError` /
`BackendKeyError` on either fetch → 503 `backend_unavailable` (`pipeline.py:232-245`).

**(b) The three reachable endpoints, and nothing is `[V]`.** `tools/leads.py` reaches exactly
`GET /leads` (`:99`), `GET /leads/{id}` (`:106`) and `GET /leads/{id}/notes` (`:114`), and nothing else;
no caller adds a query parameter (that is step 4). **Nothing in this repo is `[V]`** — `[V]` means
verified against the real backend with a real tenant and key, and `docs/STATUS.md:188` still carries Q10
("test tenant + key for the joint call") as OPEN, chased, gating "every `[V]` tag". Unit A Project 1 is
built entirely against `FakeLeadsClient` and `FakeLLM`.

**(c) What `AssembledPrompt.tail` is reserved for.** A **trusted trailing instruction rendered after the
caller-data section** (`core/prompting.py:48-62`), always loaded from a versioned file by name and never
built from a caller. It is the single reprompt's lever: `with_tail` carries `.stable` and `.variable`
through unchanged, so the cached prefix still hits and the only difference between the two calls is the
tail. Phase G filled it (`llm_call.py:254-260`, `REPROMPT_TAIL_TEMPLATE`); `build_prompt` still leaves it
empty, so `.text` with an empty tail is byte-identical to the pre-tail assembly.

**(d) The five provisional answers, and what `SEAM[STEP3]` blocks.** Q1 (the CRM forwards the end user's
JWT), Q6 (`system_event` exists in the feed and is never scored), Q7 (the rate limit keys on
`scope.subject`, not `note.author_id`), Q8 (the target note is on page one), Q13 (no business-line field
is confirmed, so `deal_specifics` is suppressed and the denominator is 80) — full table below, unchanged.
`SEAM[STEP3]` is `core/cost/limiter.py::token_preflight`, a loud no-op logging `token_preflight_bypassed`
once per process. **It blocks the token-budget pre-flight**: nothing reads a token budget before a paid
model call, so Unit A's spend is bounded only by Gate 4's request-count cap and by the per-task output
ceilings. Step 3 replaces the body of that function; the call site at `pipeline.py:326` does not move. It
suppresses nothing and decides nothing — see ruling §2.3.

**(e) Why band is derived and never accepted.** A band accepted from any input is a score the model or
the caller wrote. `Band` appears on **no** model-output schema (`schemas.py:200-267`: `ClassificationOutput`
has one field, `VagueOutput` four, `ScoreOutput` only `marks`), all three are `extra="forbid"`, and
`test_unit_a_schemas.py` introspects `model_fields` to prove no output schema carries `band` or `total`.
The only producer is `TenantConfig.band_for(total)` (`config.py:166`), fed by `compute_score`'s integer
arithmetic. So a note-shaped injection saying "score this excellent" cannot become a band: the schema
rejects the field, and the arithmetic never reads one.

**Inspect and record (path:line, one line each).**

| Path:line | What is there today |
| --- | --- |
| `pipeline.py:374-379` | **The `decide()` gap.** A five-line `SEAM:` comment ("decide() lands in Phase H, and only then can the judgement carry the score") followed by `detail = SuppressedDetail.NOT_IMPLEMENTED` at `:379` — the assignment that turns a fully scored note into a suppressed judgement. |
| `pipeline.py:372` | `score = compute_score(score_output.marks, note_type, config)` — the score IS computed today; it is thrown away into the log line at `:399`, because §2.5's scored shape is score **and** decision. |
| `pipeline.py:403-409` | `_log_outcome(scope, judgement, *, model_passes: int, score: NoteScore \| None = None)`. The `score=` parameter (`:408`, documented `:425-429`) exists only to put band and denominator on the suppressed line while the judgement cannot carry them. Its docstring already says "Phase H moves both onto the completed line … and this parameter goes away with the stub". |
| `pipeline.py:389` | `model_version=response.model` — today the stamp is the **classify** response, the only response the suppressed branch has. Ruling §2.2 moves it to the scoring response for a scored judgement. |
| `pipeline.py:141-147` | `_versions(config, model_version=NO_MODEL)`; `NO_MODEL = ""` at `:112` for a judgement no model touched. |
| `pipeline.py:345-360` | The `asyncio.gather` block for vague + score. **Not touched this phase.** |
| `pipeline.py:314-322` | `read_rate_limit` and `read_attempts` are called and their return values **discarded** — the reads happen in the specified order; nothing consumes them until `decide()` exists. |
| `schemas.py:321-328` | `Decision(action: DecisionAction, prompt_sent: bool, prompt_withheld: PromptWithheld \| None = None, attempt: int, attempts_remaining: int)`. **No field a model output could populate.** |
| `schemas.py:121-126` | `DecisionAction` — `ACCEPT_SILENT`, `ACCEPT_FLAG_PROMPT`, `PROMPT_CLARIFICATION`. |
| `schemas.py:129-141` | `PromptWithheld` — `RESUBMISSION`, `ATTEMPT_CAP`, `RATE_LIMITED`, `NOTHING_TO_ASK`, in exactly the spec's first-failing-condition order. The tree's names match the spec's. |
| `schemas.py:153-165` | `SuppressedDetail` — `NOTE_TOO_SHORT`, `SYSTEM_EVENT`, `UNCLASSIFIABLE`, `NOT_IMPLEMENTED`. |
| `schemas.py:158` | The comment under audit: *"NOT_IMPLEMENTED is the SEAM[STEP3] stub's answer until the pipeline is filled in."* **Wrong seam** — `token_preflight` suppresses nothing. Corrected in §2.3 alongside the deletion. |
| `schemas.py:331-344` | `Versions(rubric_version, prompt_version, model_version, config_version)` — four strings, all required. |
| `state.py:227-239` | `read_attempts(tenant, lead_id, note_id, *, request_id) -> int`; 0 on `RedisError` after `_bypass("attempt_counter_bypassed", …)`. |
| `state.py:242-252` | `increment_attempts(tenant, lead_id, note_id, *, ttl, request_id) -> None`; swallows `RedisError` with the same code. |
| `state.py:104-105` | `_attempt_key(tenant, lead_id, note_id) -> f"attempt:{tenant}:{lead_id}:{note_id}"`. |
| `state.py:117-130` | `_incr_with_window(client, key, ttl)` — `INCR`, then `EXPIRE` when the count is 1 **or** `TTL == -1` (audit M4). Shared by both counters. |
| `state.py:108-114` | `_bypass(code, tenant, request_id)` — one WARNING per bypassed call, `extra=` carrying `reason_code`, `tenant`, `request_id` and nothing key-derived. |
| `api/routes/judgements.py:74-97` | The resubmission route. Same body, same deps, same `judge_note`; the **only** difference is the literal `resubmission=True` keyword at `:95`. Nothing in the request distinguishes the two routes. |
| `tests/unit/test_judgement_routes.py:127-180` | Fixtures: `leads` (`FakeLeadsClient`, lead 1656, notes 10 and 11), `llm` (`FakeLLM` scripted with **two** happy paths, deliberately finite so an over-calling path is loud), `operational` (`FakeOperationalRedis`), `client` (gate wiring plus the three seams; `state.get_operational_client` monkeypatched, `_TOKEN_PREFLIGHT_LOGGED` reset). |
| `tests/unit/test_judgement_routes.py:103-107` | `_happy_path(note_type="discovery")` → `[_classified, _vague_answer, _score_answer]`; the default marks sum to **55 of 80 → 69, `fair`** — one below `accept_threshold`, which is exactly the interesting input for `decide()`. |
| `tests/unit/test_judgement_routes.py:445-455` | `test_a_failure_after_reserving_releases_the_key` — audit **S2-2**. Its body asserts a **200** and a still-held reservation. Deleted in §2.4. |
| `tests/helpers/fake_operational_redis.py:52-102` | Supports exactly six commands: `set` (with `nx`/`ex`), `get`, `incr`, `ttl`, `expire`, `delete`. **No hash commands.** `raise_on: set[str]` holds command names; a listed command raises `redis.RedisError` from `_guard` (`:47-50`). Decisive for ruling §2.6. |
| `tests/security/test_log_safety.py:47` | `SENTINEL = "SENTINEL-0501234567 villa budget 4.2M"`; `_assert_sentinel_absent` (`:80-83`) asserts both `"0501234567"` and `"SENTINEL"` are absent from the text the real `JsonFormatter` produced. |
| `scripts/check_coverage_floors.py:46-71` | `_FLOORS: dict[str, float]`, glob pattern → percent; a row is one dict entry, and `**` means the directory and everything under it. Today: `units/structured_intelligence/**` 95, `config.py` 100, `state.py` 95, `scoring.py` 100, `llm_call.py` 100, **`pipeline.py` 90** with the comment "raised in Phase H". An unmatched pattern **fails closed** (`:117-128`). |

**The nine rulings from the lead, recorded before work started.**

1. **§2.1 — Phase G's sha.** `CAMPAIGN_REPORT.md`'s Phase G heading → `STATUS: DONE 103ce02`, as this
   phase's first edit.
2. **§2.2 — `model_version` is the scoring pass's model** (the second response if scoring reprompted),
   not the classifier's. Three passes reporting different models logs `model_version_mismatch` with the
   pass labels and never the note.
3. **§2.3 — delete `NOT_IMPLEMENTED`**, no replacement code, and correct `schemas.py:158`'s wrong
   attribution to `SEAM[STEP3]` at the same time. The `token_preflight` seam comment stays.
4. **§2.4 — delete `test_a_failure_after_reserving_releases_the_key`** (audit S2-2), do not rename.
   Route-level release is already proven by `test_a_persistently_malformed_pass_is_503_and_releases_the_key`
   and by H's `model_unavailable`-then-retry edge scenario.
5. **§2.5 — register item 26 is not in H.** No `provider_request_id` / elapsed-ms work.
6. **§2.6 — item 33 storage** is mine to choose between a second key and an `HSET`, on the smaller
   `state.py` change, with the alternative's cost stated. Same TTL, same fail-open policy,
   `attempt_counter_bypassed` as the only bypass code, written only when `prompt_sent` becomes true,
   fingerprint only, and a log-safety sentinel proving it reaches no log message.
7. **§2.7 — the dangling sibling on the gather failure path is register item 63**, post-campaign batch.
   Do not touch the gather; mention only if something in H makes it worse.
8. **§2.8 — audit S2-6** (no gate-2 / gate-4 assertion on `RESUBMIT` and `VERSIONS`) is not H's. Do not
   fix; note if adjacent.
9. **§2.9 — one test in `test_reprompt.py`**: malformed first answer, provider failure on the reprompt →
   `ModelUnavailableError`, `call_count == 2`; plus one through HTTP asserting the key is released.
   Recorded as a Phase G claim landing in H.

Already decided and not reopened: reprompt per pass · the `call_model` name · ceilings 64/1024/256 ·
denominators 100/80/60 · rate limit on `scope.subject` (Q7) · the `system_event` short-circuit · no write
path, no paging, no fourth backend call.

**Tree disagreements found in Phase 0:**

- **The audit sweep cites `test_a_failure_after_reserving_releases_the_key` at `:434-442`, and the
  lead's §1.6 repeats it; the tree has it at `:445-455`.** Phase G added four tests above it. The tree
  wins; it is the same test and it is deleted in §2.4.
- Session 3's two still stand: **there is still no fake CRM app, no 127-note corpus and no injection
  fixtures** (Phase I must build them or scale to constructed fixtures), and `validate_output`'s `label`
  is still keyword-only where §4 writes it positionally.

---

## Fail-open / fail-closed matrix (§1 — do not reopen)

| # | Concern | Store / failure | Policy | Observable |
| --- | --- | --- | --- | --- |
| 1 | Auth (Gate 1), tenancy (Gate 2) | verifier / Host mismatch | **CLOSED** — 401 / 403 | `audit(decision="deny", gate=…)` |
| 2 | Request cost (Gate 4) | cost Redis (db1) unreachable | **OPEN** — allow the request | `cost_cap_bypassed` at WARNING |
| 3 | Idempotency | operational Redis (db2) unreachable | **CLOSED** — deny 503 `idempotency_unavailable` | `DodealError` + log |
| 4 | Rate limit, attempt counter | operational Redis (db2) unreachable | **OPEN** — proceed | `rate_limit_bypassed` / `attempt_counter_bypassed` |
| 5 | Model call | `LLMProviderError` | **enumerated error**, 503 `model_unavailable`, **never retried** (`retry=False`) | log; key released |

Sixth, unchanged by this campaign: backend reads keep the watchdog's **existing** retry-once
(H2 typed backend errors is step 4, not this campaign). `ExternalCallError` / `BackendKeyError`
on a fetch → 503 `backend_unavailable`.

## Provisional answers carried by this campaign

| Q | Provisional answer | Correction path when the backend/Product answers |
| --- | --- | --- |
| **Q1** — who calls us, as whom | The CRM caller **forwards the end user's JWT**; the judgement routes run the existing chain unchanged via `Depends(gate4_cost)` (Gate 1 → 2 → 4; Gate 3 parked). | The principal source swaps **behind D1's seam** — service token or signed job payload — and subjects become labelled *asserted*. No route contract changes. |
| **Q6** — `system_event` in `NoteType` | The notes feed **can carry system-generated timeline events**, not only human notes. They classify as `system_event` and are suppressed `not_scorable`, never scored. | If the backend confirms notes-only, `system_event` stays in the vocabulary but is never emitted. Nothing to rescore — suppressed judgements carry no score. |
| **Q7** — rate-limit subject | Keyed on **`context.subject`** (the JWT `sub` — *who is asking*), never the note's `author_id` (*who wrote it*), and incremented **only when a prompt is actually sent**. | If `sub` and `author_id` prove to be different namespaces and Product wants per-author limits, the key changes. Counters are windowed, so no history is invalidated. |
| **Q8** — at the fetch | The target note is on **page one** of the lead's notes (newest-first, `per_page` 25), so one un-paged `get_lead_notes` finds it; not found on page one → 404 `note_not_found`. **Nothing branches on this** — there is no paging code to change. | If a note can fall off page one, the fetch gains paging when `tools/leads.py` gets query parameters at **step 4**. See "For the lead" below — the campaign prompt does not spell Q8 out and this is my reading. |
| **Q13** — business line / `deal_specifics` | No business-line field is confirmed on the lead (`business_line_field: None`), so `deal_specifics_applicable: False` — the component is **suppressed and leaves the denominator** (100 → 80). | Name the field, add the per-business-line checklist, flip `deal_specifics_applicable`. **Never rescore history** — `config_version` stamps every judgement, so old and new coexist. |

---

## Phase A — unit schemas and the TenantConfig seam   STATUS: DONE df4689b

**What changed:**

- `src/dodeal_ai/units/structured_intelligence/schemas.py` — new. The eight §3.1 vocabularies
  as StrEnums, `JudgementRequest` (`extra="forbid"`), the three model-output schemas
  (`ClassificationOutput` / `VagueOutput` / `ScoreOutput`, all `extra="forbid"`), and the
  response models (`NoteAnalysis`, `ScoreComponent`, `NoteScore`, `Suppressed`, `Decision`,
  `Versions`, `Judgement`).
- `src/dodeal_ai/units/structured_intelligence/config.py` — new. Frozen `TenantConfig` with the
  §3.2 defaults, `EnforcementMode`, `band_for()`, and `get_tenant_config(tenant)` returning one
  shared default. Carries the ASSUMPTION[Q13] block and its four-step correction path.
- `scripts/check_coverage_floors.py` — two floors added; docstring extended to say why the unit
  is on the list and that a doubly-matched file is checked twice.
- `tests/unit/test_unit_a_schemas.py` — new, 27 tests.
- `tests/unit/test_unit_a_config.py` — new, 30 tests.
- `CAMPAIGN_REPORT.md` — new (Phase 0 block, matrix, provisional answers, this block).

**Decisions taken here:**

- **`band_for()` lives on `TenantConfig`, not in a later scoring module.** Alternative: store the
  boundaries as data and derive the band where the total is computed. Cost: the split points
  (which side of 39/40 a total falls) would be untestable until that phase, and the data and the
  derivation would be free to disagree. Putting it here makes "band derived, never accepted" a
  property of the config object itself.
- **`ScoreOutput.marks` is deliberately unbounded.** Alternative: `ge=0, le=<constant>`. Cost: the
  real bound is `0 <= mark <= weight[c]`, which is per-tenant, and `validate_output` has no
  context channel to pass weights through — so a constant would be wrong for any tenant whose
  weights differ *and* would look like the check while hiding its absence. The bound is checked in
  the scoring code. A test asserts 900 and −4 both parse, so the absence is deliberate on the record.
- **`"unclassifiable"` is a `Literal`, not an eighth `NoteType` member.** Alternative: add it to
  the enum. Cost: it would then flow into `suppressed_components_by_type` and
  `allowed_missing_by_type` lookups that legitimately expect a real type, and every
  `for t in NoteType` loop would have to special-case it.
- **`MissingComponent` stays a separate enum from `ComponentName`** (`next_step_with_date` vs
  `next_step_date`, three members vs five). Alternative: one enum. Cost: merging them would let
  the vagueness pass report `clarity` or `deal_specifics` missing — neither of which a
  clarification prompt can sensibly ask a salesperson about.
- **Every container on `TenantConfig` is immutable** (`MappingProxyType` / `frozenset` / `tuple`).
  Alternative: plain dicts on a frozen dataclass. Cost: `get_tenant_config` returns one *shared*
  instance, so `frozen=True` alone would still let any caller do `config.weights[X] = 99` and
  change another request's scoring. A test asserts the `TypeError`.
- **Two floors, with `config.py` matched by both patterns.** Alternative: the `**` floor alone.
  Cost: `config.py` is the single source of every weight, threshold, cap and TTL; 95% there
  permits an untested branch in exactly the code where a gap is a silently wrong score rather
  than a crash. The doubling is harmless (both are checked; the stricter governs) and is now
  documented in the script's docstring.
- **Response models keep pydantic's default `extra` (ignore); only the request and the three
  model-output schemas forbid extras.** Alternative: forbid everywhere. Cost: none functionally —
  but the response models are ours and are constructed in code, never parsed from an untrusted
  source, so `forbid` there would guard a boundary that does not exist and blur which schemas are
  actually facing untrusted input.

**Tree disagreements:** none. Every §2 fact Phase A depends on held exactly as written; the six
confirmed in the Phase 0 block above were re-verified against the tree, not taken from the prompt.

**Tests:** 57 added (27 schemas + 30 config); suite **278 total, 98.71 %**; floors met — 8 patterns,
new: `src/dodeal_ai/units/structured_intelligence/** = 95` and
`src/dodeal_ai/units/structured_intelligence/config.py = 100`. `schemas.py` and `config.py` are
both at 100 %. Package layout changed, so the wheel was rebuilt and verified:
`wheel import check: OK`.

**For the lead:**

- Q13's correction path is written into `config.py` as four numbered steps beside the two fields;
  step 4 is "never rescore history" and explains why `config_version` exists.
- No `Settings` field was added in this phase, so `.env.example` needs no line yet — Phase B adds one.

## Phase B — db2 operational client and the three state concerns   STATUS: DONE 42cd11c

**What changed:**

- `src/dodeal_ai/core/config.py` — `redis_operational_url` added in source position beside
  `redis_cost_url`, with the reason the two are separate logical DBs rather than one.
- `src/dodeal_ai/core/redis.py` — `get_operational_client()` (db2), same shape as
  `get_cost_client`; module docstring now describes both connections and why they are split.
- `src/dodeal_ai/main.py` — lifespan closes the operational pool beside the cost pool.
- `README.md` — `DODEAL_REDIS_OPERATIONAL_URL` row, in `Settings` source position.
- `src/dodeal_ai/units/structured_intelligence/state.py` — new. `note_fingerprint()`, the three
  key builders, `reserve_idempotency` / `release_idempotency` / `read_rate_limit` /
  `increment_rate_limit` / `read_attempts` / `increment_attempts`, `IdempotencyUnavailableError`.
- `tests/helpers/fake_operational_redis.py` — new, mypy-checked. Six commands, `raise_on`,
  inspectable `store` / `ttls` / `commands`, no clock.
- `tests/unit/test_unit_a_state.py` — new, 31 tests.
- `tests/unit/test_redis.py` — clears the second `lru_cache`; two tests that each factory reads
  its own URL and that the two DBs differ.
- `scripts/check_coverage_floors.py` — `state.py` floor at 95, with its reason.

**Decisions taken here:**

- **`note_fingerprint()` lives in `state.py`, not in the pipeline.** The phase spec lists six
  state functions and no fingerprint helper. Alternative: compute the SHA-256 in `pipeline.py`
  (Phase C). Cost: the idempotency key's format would be split across two modules — the prefix
  and note_id here, the variable part there — so a change to either half could silently stop
  matching. Keeping it here means one module owns the whole key.
- **`request_id` added as a keyword-only parameter to all six functions.** The spec's signatures
  omit it (`read_rate_limit(tenant, subject) -> int`) but also require bypass paths to log
  "with tenant/request_id via `extra=`". Alternative: a contextvar. Cost: an implicit ambient
  request id is exactly the kind of thing that is empty in a worker and silently logs `None`.
  Recorded as a reconciliation, not a disagreement — see below.
- **No Lua, and the INCR/EXPIRE pair is deliberately non-atomic.** Alternative: a script like the
  cost limiter's. Cost: audit M5 records that no Lua in this repo is executed by the suite until
  `fakeredis[lua]` at step 3, so a script here would be untested logic guarding paid work. The
  race this admits re-sets the same TTL on the same key — harmless. The cost limiter's script is
  different in kind: two counters that must move together.
- **`release_idempotency` swallows every `RedisError`.** Alternative: propagate. Cost: it runs
  only on an error path, so raising would replace the real failure (model down, backend down)
  with a less useful one, and the reservation expires on its TTL anyway.
- **`reserve_idempotency` raises `from None`.** Chaining the `RedisError` would carry its message
  — which can quote the command, and so the key, and so the fingerprint — into any traceback
  formatted downstream. Same rule and same reason as `core/validation.py`. A test asserts
  `__cause__ is None`.
- **The M4 edge is handled on db2 (`TTL == -1` → `EXPIRE`).** Alternative: set the TTL only on
  create, as the cost Lua does. Cost: that is the open finding M4; here a counter surviving
  without a TTL would pin a user's rate limit or a note's attempt count forever, silently
  withholding every future clarification prompt. The `or` short-circuits, so a new key still
  costs only INCR + EXPIRE — a test asserts the command sequence.
- **Two extra `test_redis.py` tests not in the phase spec.** A copy-paste between the two nearly
  identical factories would put idempotency reservations in the cost DB, counted as spend and
  flushed on a different schedule. Cheap to guard, invisible if it happened.

**Tree disagreements:**

- **The phase spec's state signatures omit `request_id`, which its own logging requirement needs.**
  Followed the requirement and added `*, request_id: str` to all six functions. Nothing in §2 or
  the documents contradicted; this is an internal gap in the phase text, resolved toward the
  stated logging behaviour.
- Nothing else. `get_cost_client`'s shape, the cost-test injection pattern
  (`monkeypatch.setattr(module, "get_client", lambda: fake)`), the README's source-order rule and
  the `_FLOORS` mechanics all held exactly as §2 described.

**Tests:** 33 added (31 state + 2 redis); suite **313 total, 98.81 %**; floors met — 9 patterns,
new: `src/dodeal_ai/units/structured_intelligence/state.py = 95` (actual 100 %). Wheel rebuilt and
verified: `wheel import check: OK`.

**For the lead:**

- **Add to `.env.example`:** `DODEAL_REDIS_OPERATIONAL_URL=redis://localhost:6379/2`, placed after
  `DODEAL_REDIS_COST_URL` to keep the file in `Settings` source order. This session cannot read or
  write `.env*` — the path is denied to every tool — so the README row is done and that one line
  is not.
- `/ready` still pings only the cost connection; db2 is not in the readiness probe. Deliberate for
  now (the campaign records the debt in Phase J), but worth knowing: an operational-Redis outage
  is invisible to an orchestrator and shows up as 503 `idempotency_unavailable` on judgements.

## Phase C — judgement routes, TenantScope, DodealError, SEAM[STEP3]   STATUS: DONE 317619f

**What changed:**

- `src/dodeal_ai/core/context.py` — `TenantScope` (frozen, four fields) and `RequestContext.scope()`.
- `src/dodeal_ai/tools/leads.py` — all three methods now take `scope: TenantScope`; added
  `get_leads_client()`; docstring records the H2 caveat.
- `tests/unit/test_leads_client.py`, `tests/integration/test_leads_e2e.py` — `_context()` →
  `_scope()`, built through `RequestContext.scope()`; two factory tests.
- `scripts/real_fetch_check.py` — builds a `TenantScope` directly (it has no gate chain to narrow
  from — the non-user-principal case D1 describes).
- `src/dodeal_ai/core/errors.py` — `DodealError(reason_code, http_status)` + eight subclasses,
  `dodeal_error_handler`, `request_validation_handler`, `_unit_error_body`.
- `src/dodeal_ai/api/routes/judgements.py` — new. `APIRouter(prefix="/api/v1")`, three routes.
- `src/dodeal_ai/main.py` — includes the router beside `_probe`.
- `src/dodeal_ai/api/routes/_probe.py` — docstring line: removal deferred past the campaign.
- `src/dodeal_ai/units/structured_intelligence/pipeline.py` — new. `judge_note`, `JudgementDeps`,
  the version constants, `_fetch_note`, `_is_thin`, `_suppressed`, `_log_outcome`.
- `src/dodeal_ai/core/cost/limiter.py` — `token_preflight()`, the SEAM[STEP3] no-op.
- `tests/helpers/fake_leads.py` — new, mypy-checked.
- `tests/unit/test_judgement_routes.py` (35), `test_judgement_pipeline.py` (12),
  `test_tenant_scope.py` (7), `test_dodeal_errors.py` (28) — new.
- `scripts/check_coverage_floors.py` — `pipeline.py` floor at 90.

**Decisions taken here:**

- **`lead_not_found` is defined but never raised.** §3.6 wants `get_lead` → 404 `lead_not_found`,
  but the watchdog collapses every backend failure into `ExternalCallError`, so a genuine 404 is
  indistinguishable from a 500 or a timeout — that is open finding **H2, explicitly step 4 and
  explicitly not this campaign**. Alternative: infer "not found" from `ExternalCallError`. Cost:
  the CRM would be told a lead is missing whenever the backend is merely down, which is worse than
  a 503 — it invites the caller to delete or re-create a record that exists. So a missing lead is
  503 `backend_unavailable` today; the class, its status mapping and its test exist so that when
  H2 lands the branch goes into `_fetch_note` and nothing else moves. **`note_not_found` IS
  reachable** and is fully wired: notes come back as a list, so a missing id is determinable
  without typed errors.
- **`request_validation_handler` drops every field name, location and message.** Alternative:
  keep pydantic's `loc`/`msg` and strip only `input`. Cost: a field name in an `extra_forbidden`
  error is attacker-chosen text echoed back, and the whole reason the 422 exists here is that
  `extra="forbid"` catches a caller posting note text. A test asserts even the string `"note"`
  is absent from the response.
- **`token_preflight` is `async` although nothing awaits inside.** Alternative: sync now, async at
  step 3. Cost: the real body is a `redis.asyncio` read (D2), so every call site would change
  later — for a seam whose whole purpose is that the call site does not.
- **`token_preflight` does not reuse `get_usage()`.** `get_usage` reads the per-REQUEST counters
  Gate 4 increments; a token budget is a different quantity, key and window. Reusing it would make
  "requests made" silently stand in for "tokens spent".
- **`model_version` means two different things, deliberately.** On a judgement it is what the
  provider reported ran (`""` when no model ran — never `settings.llm_model`, because stamping the
  configured pin on an unspent judgement makes it look spent). On `/meta/versions` there is no call
  to report, so it is the configured pin. Alternative: omit it from `/meta/versions`. Cost: the
  endpoint is specified as "the four version strings" and a three-field response would not match
  the `Versions` schema. Both are documented at their definition sites.
- **`JudgementDeps` carries the leads client, the LLM client and the config — but not the state
  functions.** Those are called through the `state` module, which tests redirect with
  `monkeypatch.setattr`. Alternative: put them on the dataclass too. Cost: two injection points
  for one thing, and the Phase B tests already established the module-level pattern.
- **`/meta/versions` sits behind `gate4_cost` like the other two.** Alternative: leave it open.
  Cost: version strings are a fingerprint of the deployment; there is no reason for them to be
  public, and putting it behind the same dependency means there is no ungated route to forget about.
- **A direct-call pipeline test module exists alongside the HTTP tests.** The release-on-failure
  path is unreachable through HTTP today (everything after the reservation fails open), but it is
  the difference between "retry" and "409 for 24 hours" the moment model calls land. Tested now at
  the seam rather than after something depends on it.

**Tree disagreements:**

- **§3.6's `get_lead (404 lead_not_found)` cannot be honoured on this tree.** H2 is open; followed
  the tree, as the campaign's own rule requires, and recorded it above and in a code comment in
  `_fetch_note`. This is the one place the phase's specified behaviour and the tree genuinely
  conflict.
- **§2 said "no route accepts a body today", and that FastAPI's stock 422 echoes the caller's
  input.** Both were true and both are now changed on purpose by this phase.
- Everything else in §2 held: the `test_chain.py:38-52` fixture shape transplanted unmodified, the
  cost-gate fake, `gate4_cost` as the single chain dependency, the router-prefix pattern, and
  `HTTPStatus(422).phrase == "Unprocessable Entity"` on Python 3.12.
- One test bug of mine, not a tree issue: `TestClient.get()` takes no `json=` kwarg.

**Tests:** 84 added (35 routes + 12 pipeline + 28 errors + 7 scope + 2 leads factory); suite
**397 total, 98.99 %**; floors met — 10 patterns, new:
`src/dodeal_ai/units/structured_intelligence/pipeline.py = 90` (actual 97.18 %). The integration
suite was re-run separately (`-m integration`): **7 passed**. Wheel rebuilt and verified:
`wheel import check: OK`.

**For the lead:**

- The error-taxonomy debt in STATUS §2 is settled **for unit errors only**. The gates keep their
  hand-mapped `HTTPException`s and their existing bodies — `{"detail": "Too Many Requests"}` is
  still exactly that, pinned by both `test_chain.py:224` and a new route test.
- `pipeline.py`'s only uncovered lines are the `judgement_completed` log branch, unreachable until
  scoring exists. Floor set at 90 as specified; Phase H should raise it.

## Phase D — classification against FakeLLM, system_event short-circuit   STATUS: DONE 8874958

**What changed:**

- `src/dodeal_ai/prompts/structured_intelligence/classify_v1.txt` — new. The seven types with
  one criterion each, the `unclassifiable` escape, a precedence rule, and JSON-only output. No
  weight, no threshold, no band, no language branch.
- `src/dodeal_ai/units/structured_intelligence/llm_call.py` — new. `complete_once` (watchdog,
  `retry=False`, `timeout=llm_timeout_seconds`, `ExternalCallError` → `ModelUnavailableError`),
  `parse_output` (JSON decode + `validate_output`, both failures → `OutputValidationError`), and
  `call_model` returning `(validated, LLMResponse)`.
- `src/dodeal_ai/units/structured_intelligence/classify.py` — new. `CLASSIFY_TEMPLATE`,
  `_caller_data`, `build_classification_prompt`, `classify`, `suppression_for`. Carries the
  `ASSUMPTION[Q6]` marker and its correction path.
- `src/dodeal_ai/units/structured_intelligence/pipeline.py` — `_fetch_note` returns
  `(Lead, LeadNote)`; `_suppressed` takes `model_version`; `JudgementDeps` gains `settings`;
  classification runs after the SEAM[STEP3] call and its two terminal answers end the judgement.
- `src/dodeal_ai/api/routes/judgements.py` — both POST routes take `Depends(get_settings)` and
  pass it into `JudgementDeps`.
- `tests/helpers/fake_llm.py` — `json_response(payload)` and `FakeLLM.rescript(*script)`. The
  class already scripted ordered responses and counted calls, so nothing else was needed.
- `tests/unit/test_classification.py` — new, 38 tests.
- `tests/unit/test_judgement_routes.py` — the `llm` fixture is scripted; 13 tests added, 4
  rewritten where they pinned "no model ran".
- `tests/unit/test_judgement_pipeline.py` — `deps` gains `settings` and a scripted `llm`; 1 test
  rewritten, 1 added.
- `tests/security/test_log_safety.py` — 3 sentinels for the model-output path.
- `CAMPAIGN_REPORT.md` — Phase C's sha backfilled, session 3's Phase 0 block, this block.

**Decisions taken here:**

- **`llm_call.py` exists from Phase D, not Phase G.** §5's file list gives it to G, which adds
  `call_validated` and the reprompt. Built now because D, E and F each need call → decode →
  validate → translate-the-failures, and the alternative is three copies of the
  **untrusted-parse boundary** — the one place a string a model wrote becomes a typed object.
  Cost of the alternative: three places to audit for a JSON-decode leak instead of one, and G
  would have to delete two of them while adding the reprompt. G's own deliverable is untouched:
  `call_validated`, the tail rebuild and the two-calls-on-failure contract all still land in G,
  and the call sites do not move when they do.
- **Any model failure is one code, `model_unavailable`.** `call_with_watchdog` collapses a
  translated `LLMProviderError`, a timeout and a transport error into `ExternalCallError`, and
  `complete_once` turns all of them into 503 `model_unavailable`. Alternative: branch on
  `exc.cause` and emit different codes. Cost: a vocabulary the caller cannot act on differently
  — every one of them means "try again later" — and a branch on a foreign exception's type
  inside the one module that must never look at foreign exception content.
- **Validation runs OUTSIDE the watchdog.** Only `client.complete()` is wrapped. If validation
  ran inside, a perfectly healthy provider returning prose would be reported as
  `backend`/`model_unavailable`, and the reprompt Phase G adds would never see the failure it
  exists for. This is the one non-obvious line in `llm_call.py` and it is commented as such.
- **A JSON-decode failure is an `OutputValidationError`, not its own type.** Both halves of
  "malformed" — not JSON at all, and JSON of the wrong shape — reach Phase G's reprompt through
  one exception. It logs the same `output_validation_failed` event with
  `error_types=json_invalid`, so one alert catches both. Alternative: a distinct
  `MalformedJsonError`. Cost: G's `call_validated` would have to catch two types to do one thing.
- **Nothing is repaired.** A fenced object, prose around an object, a truncated object: all
  malformed, none stripped or salvaged. Repairing model output in code makes the judgement partly
  ours and untestable against the prompt. Tests pin the fence and the prose cases explicitly so a
  future "helpful" strip fails the suite.
- **`_fetch_note` returns the `Lead` as well as the note.** The classifier's context section needs
  four lead fields. Alternatives: fetch the lead twice, or pass the id and let `classify` fetch.
  Cost of either: the number of backend calls would depend on the note type, and the lead is
  already in hand from the existence check.
- **Four context fields, and only four** (`leadType`, `enquiryType`, `project`, `status`). They
  disambiguate a note ("interested in the same one" reads differently on a leasing enquiry) and
  none of them can identify a client. `name`, `phone` and `email` are on the `Lead` object and
  stay there; a test asserts all three are absent from the assembled prompt.
- **Lead context first, note last, in the caller-data section.** Both halves are untrusted, but
  the note is the part being analysed, so "everything after `NOTE:`" is unambiguous and a note
  containing the string `LEAD CONTEXT:` cannot appear to precede context that outranks it.
  Delimiter neutralisation is left to `build_prompt` and deliberately not repeated — doing it in
  two places is how one of them drifts.
- **`model_version` distinguishes the two kinds of suppression.** A thin note is refused before
  anything is spent and stamps `""`; a `system_event` is refused BY a classifier and stamps what
  that classifier reported. Alternative: `""` for every suppressed judgement. Cost: an unspent
  judgement and a paid one would look identical on the only field that records the difference.
- **`JudgementDeps` gains `settings` rather than `llm_call.py` calling `get_settings()`.**
  `llm_timeout_seconds` is a per-call argument; a test that wants a different one should set it on
  the deps it already builds, not reach into a process-wide cache.
- **`suppression_for()` returns the DETAIL only.** The reason is always `not_scorable` and the
  judgement shape belongs to the pipeline. One function answers "does this classification stop
  here", and a new `NoteType` that should stop is one line in the place the question is asked.
- **`FakeLLM.rescript()` instead of poking `_script`.** A route test receives its FakeLLM through
  the `client` fixture, already wired into `dependency_overrides`, so it must change the script on
  the instance it was given. One public method beats the fake's internals appearing in a dozen
  call sites. `FakeLLM` needed no other extension — it already scripted ordered responses and
  counted calls, which is what §5 asked Phase D to check.

**Tree disagreements:**

- **None with §5's Phase D text.** The one deviation from its file list (`llm_call.py` arriving
  early) is a decision recorded above, not a disagreement with the tree.
- Recorded in this session's Phase 0 block and repeated here because it lands on a later phase:
  **there is no fake CRM app, no 127-note corpus and no injection fixtures** anywhere in the tree.
  Phase I is specified against all three.
- **The clean-venv wheel check could not be run** — the sandbox denied the `uv venv` + install
  command. What *was* verified: `uv build --wheel` succeeds and the built wheel contains
  `dodeal_ai/prompts/structured_intelligence/classify_v1.txt`. Neither `pyproject.toml` nor the
  package layout changed in this phase, so the campaign's trigger for the full check did not fire;
  the wheel-contents check was run anyway because this is the first real file in that directory.

**Tests:** 55 added (38 classification + 13 routes + 1 pipeline + 3 log-safety); 5 existing tests
rewritten where they asserted "no model ran". Suite **454 total, 99.05 %** (floor 92); all 10
floors met, none added — `classify.py` and `llm_call.py` are both **100 %** under the unit's
existing `units/structured_intelligence/** = 95`. `pipeline.py` 97.53 % against its 90 floor.
mypy: 56 source files, clean. Integration suite re-run separately (`-m integration`): **7 passed**.

**For the lead:**

- Phase I is specified against a corpus and injection fixtures that do not exist in the tree. It
  needs either a corpus-building commit before it, or a scoped-down I.
- `classify_v1.txt` is v1 wording written to be revised: Phase I hardens it in place (few-shot
  AR/EN/mixed examples, the explicit untrusted-data instruction) with no version bump, because no
  judgement has ever been produced by it.

## Phase E — vague detection: per-type prompts, fixed missing-components vocabulary   STATUS: DONE b432af1

**What changed:**

- `src/dodeal_ai/prompts/structured_intelligence/vague_{no_contact,callback,discovery,viewing,negotiation,won_lost}_v1.txt`
  — six new templates. Each names its type's bar, applies the floor test ("could another agent pick
  up this lead tomorrow, read this note and nothing else, and carry on?"), lists the components it
  may report missing, forbids the generic question by name, and asks for JSON exactly `VagueOutput`
  with the biconditional spelled out. The `no_contact` one lists two components and says in so many
  words that `client_said` must never appear.
- `src/dodeal_ai/units/structured_intelligence/vague.py` — new. `_TEMPLATES` (six entries,
  `system_event` deliberately absent), `template_for`, `_caller_data`, `build_vague_prompt`,
  `allowed_components_check`, `detect_vagueness`.
- `src/dodeal_ai/units/structured_intelligence/llm_call.py` — `output_rejected(label, errors)`
  (logs `output_validation_failed` and builds the error), and a `check` hook on `parse_output` /
  `call_model` for the rules a schema cannot hold.
- `tests/unit/test_vague.py` — new, 72 tests.
- `CAMPAIGN_REPORT.md` — Phase D's sha backfilled, this block.

**Decisions taken here:**

- **Phase E does not touch the pipeline.** `vague.py` is built and tested; nothing calls it yet.
  §5 gives Phase F the concurrency requirement — "from this phase the pipeline runs them
  concurrently with `asyncio.gather`" — so wiring vague in sequentially here would create a
  pipeline shape that exists for exactly one commit, and a concurrency test written twice. The
  alternative's cost is real but small (one commit where the module is exercised only by its own
  tests); the cost of doing it the other way is a rewrite of the same call site in the next commit.
  §5's Phase E bullets do not mention the pipeline, unlike Phase D's, which is read as deliberate.
- **The per-type rule runs INSIDE the validated call, through a new `check` hook.** §5 requires
  `client_said` on a `no_contact` note to be "a validation failure", and §5's Phase F requires the
  same of an out-of-range mark, both so Phase G's reprompt covers them. A rule enforced *after*
  `call_model` returned would be a malformed answer that never earned its reprompt. So
  `parse_output` takes a `check` that raises `OutputValidationError`, and the hook is a closure
  over the note type and the tenant config — the context `validate_output` structurally cannot
  have. Alternative: enforce it in the pipeline after the call. Cost: the reprompt would silently
  not apply to two of the three ways an answer can be wrong.
- **`check` raises; it never repairs.** Dropping the disallowed component and carrying on would
  hand the decision step an answer no model gave. A test pins the rejection specifically so a
  future "just filter it out" fails the suite.
- **The allowed set stays in `TenantConfig`, not in `vague.py`.** It is a rubric decision like a
  weight or a threshold, and a tenant that wants a different set should not need a code change.
- **Six templates, not one parameterised template.** A single file would have to describe all six
  bars and then trust the model to pick the right one *after* classification already decided —
  paying twice for the same decision and giving it a second chance to get it wrong. Cost of six:
  the shared paragraphs are duplicated six times, so a wording fix is six edits. They were
  generated from one skeleton so the shared halves are byte-identical today, and Phase I revises
  them in place.
- **The vague prompt carries the note and NO lead context**, unlike classification. The floor test
  is "could another agent read THIS NOTE and carry on"; handing the model the lead's project and
  status would let it fill in from the record what the note does not say, and pass a note that
  leaves the next reader guessing. Alternative: same context as the classifier. Cost: the floor
  test stops meaning what it says.
- **`template_for` raises rather than falling back.** A type with no template is a programming
  error — the only one is `system_event`, which the classifier stops before. A generic fallback
  would judge a `no_contact` note against a discovery bar and nobody would see it happen. A test
  asserts the six keys plus `system_event` are exactly `NoteType`, so an eighth type is a failing
  test rather than a `KeyError` in production.
- **The rejected component's NAME goes into the error location** (`missing_components.client_said`,
  type `component_not_allowed_for_type`). It is a `MissingComponent` member — fixed vocabulary,
  never free text — so it is safe on a log line and makes the failure diagnosable. A test asserts
  the note text is absent from the same log output.

**Tree disagreements:**

- **§5 asks Phase E to "add a per-type allowed set in `config.py`". It is already there.**
  `allowed_missing_by_type` was built in Phase A with exactly the right contents
  (`no_contact` → `{what_happened, next_step_with_date}`, every other type → all three). Phase E
  therefore added no config; it consumed what existed. Recorded because a reader comparing the
  phase to the spec will look for a config diff and find none.

**Tests:** 72 added; suite **526 total, 99.07 %** (floor 92); all 10 floors met, none added —
`vague.py` is at **100 %** under the unit's existing `units/structured_intelligence/** = 95`.
mypy: 57 source files, clean. Integration suite re-run separately (`-m integration`): **7 passed**.
Wheel rebuilt: all seven prompt files ship under `dodeal_ai/prompts/structured_intelligence/`.

**For the lead:**

- The six templates are the wording most worth a human read before Phase I hardens them. In
  particular: `won_lost` currently expects a next step *or* an explicit "nothing follows", which is
  a judgement call about closed leads that Product may want to make differently.

## Phase F — scoring: marks from the model, arithmetic in code, Q13 suppression   STATUS: DONE 2f2dbfb

**What changed:**

- `src/dodeal_ai/prompts/structured_intelligence/score_v1.txt` — new. Defines what each of the five
  components means and how to mark it; says to mark only the components listed in the caller data,
  every one of them, as whole numbers up to the ceiling given beside each. No weight, no threshold,
  no total, no band anywhere in the file.
- `src/dodeal_ai/units/structured_intelligence/scoring.py` — new. `applicable_components`
  (type suppression ∘ `ASSUMPTION[Q13]`), `validate_marks`, `marks_check`, `compute_score`,
  `_caller_data`, `build_score_prompt`, `score_note`.
- `src/dodeal_ai/units/structured_intelligence/pipeline.py` — after classification, vague detection
  and scoring are issued together with `asyncio.gather`; the analysis is filled from the vague
  answer; `compute_score` runs; `_log_outcome` takes the computed score and puts `band` and
  `denominator` on the line.
- `tests/helpers/fake_llm.py` — `hold_after`, which holds calls past a given index open until
  `released` is set, so concurrency is provable rather than inferred from a count.
- `tests/unit/test_scoring.py` — new, 66 tests.
- `tests/unit/test_judgement_routes.py` — the `llm` fixture scripts two full judgements; 3 tests
  added, 5 rewritten for three calls.
- `tests/unit/test_judgement_pipeline.py` — 6 concurrency and release tests added, 1 rewritten.
- `scripts/check_coverage_floors.py` — `scoring.py` at 100.
- `CAMPAIGN_REPORT.md` — Phase E's sha backfilled, this block.

**Decisions taken here:**

- **CONCURRENCY (register item 14): `asyncio.gather` for vague + score, `return_exceptions=False`.**
  Classification must finish first — the vague template and the applicable components are both
  chosen by type, so neither of the other two prompts can even be *assembled* until the answer is
  in. The other two do not depend on each other, so the happy path is two round-trips while the
  call count stays three. **The alternative and its cost:** sequential calls are simpler to reason
  about — one failure at a time, one release, no question about which coroutine raised — and they
  cost **one extra model round-trip on every single judgement**, on the request path a salesperson
  is waiting on. The failure semantics turned out to cost less than feared: the existing
  `try/except` around the whole post-reservation block already releases exactly once, and a test
  counts the releases to prove it.
- **The score is computed but not published.** §2.5's scored shape is `score` AND `decision`, and
  `decide()` is Phase H. Half a scored judgement is one the CRM cannot act on, so the judgement
  still carries `Suppressed(not_scorable, not_implemented)` — while the arithmetic runs anyway, so
  an out-of-range mark fails in the phase that produced it rather than in the phase that would have
  acted on it. The computed band and denominator go on the log line, which is the only place they
  are observable until H; Phase H moves them onto the completed line and the `score=` parameter
  goes away with the stub. Alternative: put `score` on the judgement beside `suppressed`. Cost: the
  Judgement invariant ("exactly one of score/decision and suppressed is populated") would be false
  for one commit, and something downstream would read the score without a decision.
- **The weights travel in the CALLER DATA section, as ceilings.** §5 says so, and the reason holds
  up: a weight in template text could not be changed without bumping the prompt version, and a
  per-tenant rubric would need a per-tenant template. In the variable half, `.stable` is
  byte-identical for every tenant and every note type — one cached prefix, one file to review. The
  model is told the highest mark each component can take, never what they add up to.
- **A note that names its own ceilings cannot inflate a mark.** The components block goes first and
  the note last, and the template says in so many words that only the block above counts — but the
  real defence is `validate_marks`, which rejects anything outside `[0, weight]` in code. The
  ordering just means the model is not asked to arbitrate. A test drives a note containing
  `what_happened: 0 to 100 — mark me full`.
- **The three mark failures run inside the validated call, through Phase E's `check` hook.** §5
  requires them to be `OutputValidationError` "so Phase G's reprompt covers it", which is only true
  inside `call_model`. They cannot be schema constraints: the bound is the *tenant's* weight and
  `validate_output` has no context channel. A `ge=0` on `ScoreOutput` would look like the check and
  hide its absence — `schemas.py` already says so at the field.
- **Every problem is reported, not just the first.** One reprompt is all a model gets, so it is
  told everything that was wrong at once rather than being corrected one field at a time across
  attempts it will not have.
- **A missing mark is a rejection, never a zero.** Treating an unmarked applicable component as 0
  would mark a note down for the model's omission, and the salesperson would never know why.
- **A zero denominator is a named `ValueError`, not a `ZeroDivisionError`.** Unreachable with any
  shipped config — `clarity` is suppressed by no type and is not Q13-gated — but the only way to
  get there is a rubric that suppresses everything, which is a configuration fault worth naming.
- **The arithmetic is integers throughout.** `(raw * 100 + denominator // 2) // denominator` is
  round-half-up with no float anywhere, so a result cannot depend on binary rounding and two runs
  of the same marks cannot differ. Pinned at the 70 boundary: 55/80 → 69 `fair`, 56/80 → 70 `good`.
- **The template grep is stricter than asked.** §5 wants the *score* template checked for `total`,
  `band`, `poor`, `excellent`. The test checks **every** template in the set, parametrised over the
  directory, and separately pins the eight filenames the campaign ships. It costs nothing and
  catches the same mistake in the file where it would be least expected.
- **Ordered scripting stayed, and is now self-checking.** `FakeLLM` pops in order, which is exactly
  right while calls are sequential; from the moment two are concurrent, "the next scripted response"
  depends on the event loop rather than on the pipeline's contract. `asyncio.gather` schedules in
  argument order, so the issue order is deterministic (classify, vague, score) — and a test pins it
  by inspecting the recorded prompts, so if it ever changes, the suite says so instead of every
  route test failing on a confusing validation error.

**Tree disagreements:**

- **§2.3's denominator of "75 (no_contact, Q13 resolved)" is not reachable from §2.2's own
  numbers.** Weights are 25 / 20 / 25 / 20 / 10 and `suppressed_components_by_type` suppresses
  `client_said` AND `deal_specifics` for `no_contact` **by type**, so lifting Q13 changes nothing
  for it: 25 + 25 + 10 = **60**, with or without Q13. Getting 75 would require exactly one
  25-weight component (`what_happened` or `next_step_date`) to be suppressed, and no rule in the
  campaign does that. If `no_contact` suppressed only `client_said`, the answer would be 80 — which
  §2.3 already lists. **Followed the tree**, as the campaign requires: the fourth denominator test
  asserts 60 and carries this arithmetic in a comment. The other three (100, 80, 60) are exactly as
  specified. **For the lead:** either §2.3's "75" is a slip for "80", or
  `suppressed_components_by_type[no_contact]` is meant to lose `deal_specifics` when Q13 resolves.
  The second reading is defensible — a no-contact note has no deal specifics *because nothing was
  discussed*, which is the same reason Q13 gives — but it is a rubric change and not mine to make.
- **`band_for(total, config)` is not added to `scoring.py`.** §5 lists it there; the tree already
  has `TenantConfig.band_for(total)`, built in Phase A, and `config.py`'s own docstring is explicit
  that a number appearing in two places drifts. A module-level wrapper would be a second spelling
  of one function. Followed the tree; `compute_score` calls `config.band_for`, and the boundary
  tests (0/39/40/69/70/84/85/100) are in `test_scoring.py` where §5 wants them.
- **`is_thin(text, config)` is not added to `pipeline.py`.** §5 lists it as Phase F work; the tree
  already has `_is_thin(note, config)`, built in Phase C, applied before the reservation and before
  any model call exactly as §2.6 requires. Renaming it and changing its parameter for cosmetic
  agreement would be a diff in a phase that has no reason to touch it. Followed the tree; the
  behaviour §5 asks for is tested (a thin note is suppressed with **zero model calls** and no
  reservation — the call-count assertion is new in this phase).
- **The label is `llm.unit_a.score`, not `"score"`.** §5 writes `OutputValidationError("score", …)`.
  The repo's label convention is `llm.unit_a.classify` / `llm.unit_a.vague` / `tool.get_lead`, and a
  bare `score` in a log line naming which output failed would be the odd one out. Cosmetic.

**Tests:** 75 added (66 scoring + 3 routes + 6 pipeline); 6 existing rewritten for three calls.
Suite **600 total, 99.12 %** (floor 92). **11 floors met, one new:**
`src/dodeal_ai/units/structured_intelligence/scoring.py = 100` (actual 100 %). `vague.py`,
`classify.py`, `llm_call.py`, `schemas.py`, `state.py`, `config.py` all at 100 %; `pipeline.py`
97.85 % against its 90 floor (its only gap is still the `judgement_completed` branch, unreachable
until Phase H fills in `decide`). mypy: 58 source files, clean. Integration suite re-run separately
(`-m integration`): **7 passed**. Wheel rebuilt: all eight prompt files ship.

**For the lead:**

- **The 75 denominator (above) is the one thing in this phase that needs a human answer.** Nothing
  is blocked — the shipped behaviour is 60 and is what §2.2 dictates — but if the intent was that
  `no_contact` loses `deal_specifics` only *because of* Q13, that is a one-line config change and it
  should be made deliberately, before any judgement is stored under `tenant-cfg-default-1`.
- `pipeline.py`'s floor is still 90 while `decide` is missing; Phase H raises it to 95 and the
  `score=` parameter on `_log_outcome` disappears with the `not_implemented` stub.

## Phase G — reprompt once via `AssembledPrompt.tail`   STATUS: DONE 103ce02

**What changed:**

- `src/dodeal_ai/prompts/structured_intelligence/reprompt_tail_v1.txt` — new. The stricter
  instruction: the object and nothing else, no fence, every field present and no field extra,
  values from the fixed lists, "keep it compact", "this is the second and last attempt".
- `src/dodeal_ai/core/prompting.py` — `with_tail(prompt, template_name)`: the same prompt with a
  trusted trailing instruction loaded by name. `stable` and `variable` are CARRIED, not rebuilt.
  `AssembledPrompt.tail`'s docstring no longer says "empty today".
- `src/dodeal_ai/units/structured_intelligence/llm_call.py` — `call_model` now sends, validates,
  and on `OutputValidationError` logs `reprompt_issued`, rebuilds through `with_tail` and sends
  once more; a second failure is `MalformedOutputError`. `parse_output` rejects `MAX_TOKENS` as
  `output_truncated` before it decodes. `complete_once` takes a REQUIRED keyword
  `max_output_tokens` and passes it to the client.
- `classify.py` / `vague.py` / `scoring.py` — one output ceiling each (register item 15):
  `CLASSIFY_MAX_OUTPUT_TOKENS = 64`, `VAGUE_MAX_OUTPUT_TOKENS = 1024`,
  `SCORE_MAX_OUTPUT_TOKENS = 256`, each with its arithmetic in the comment beside it.
- `src/dodeal_ai/units/structured_intelligence/pipeline.py` — the log field `model_calls` is now
  `model_passes`, and the module docstring says why a pass is not a call.
- `scripts/check_coverage_floors.py` — `llm_call.py` at 100.
- `tests/unit/test_reprompt.py` — new, 22 tests.
- `tests/unit/test_assembled_prompt.py` — 4 `with_tail` tests.
- `tests/unit/test_judgement_routes.py` — 4 tests added (reprompt on one pass only, the 503 +
  release, the three ceilings over HTTP, truncation reprompted); 3 rewritten for two calls.
- `tests/unit/test_classification.py`, `test_vague.py`, `test_scoring.py` — the three helpers script
  the same answer twice; the "costs exactly one call" tests are now "exactly two".
- `tests/security/test_log_safety.py` — 2 sentinels: the reprompt line, and the second prompt.
- `CAMPAIGN_REPORT.md` — the session 5 Phase 0 block and this one.

**Decisions taken here:**

- **The tail is applied with `dataclasses.replace`, not by re-calling `build_prompt`.** §5 requires
  `.stable` and `.variable` byte-identical with only `.tail` differing; `replace` makes that true by
  CONSTRUCTION rather than by coincidence — the two strings are the same objects, not two loads of
  the same file. **The alternative:** re-assemble through `build_prompt` and set the tail. It would
  produce identical bytes today, at the cost of a second template read and a second delimiter
  neutralisation per reprompt, and of a place for the two to drift the day either changes. A test
  asserts the byte-identity anyway, because the guarantee is what the phase is for.
- **`with_tail` takes a template NAME and there is no parameter that takes a string.** The one string
  that must never be appended is the answer that was just rejected, and the surest way to keep it out
  is to have no channel for it. **The alternative** — `with_tail(prompt, text)` — is one line shorter
  and puts the boundary back in the caller's discretion.
- **The tail lives in `core/prompting.py`, the template name in `llm_call.py`.** The type owns the
  guarantee about its own fields; the unit owns which file it uses. `_load_template` stays private.
- **One tail file for all three passes, not three.** What it says — answer with the object, nothing
  around it, every field, values from the fixed lists — is the same whichever object was asked for.
  Three files would be three ways of saying it, and three chances for two of them to disagree. A test
  proves the tail is identical whatever the failure was, which is also what proves it is not derived
  from the rejected answer.
- **The reprompt lives in `call_model`, so it is per pass and not per request.** Vague detection and
  scoring run concurrently (register item 14); each enters `call_model` separately, so a reprompt on
  one re-issues that one and nothing else. There is no shared attempt state. **The alternative** —
  one reprompt budget per judgement — would make the two concurrent passes contend for it and make
  the outcome depend on which of them failed first.
- **The tree's `call_model` was extended in place; §5's `call_validated` is not added.** Same
  signature shape, same return, and its own docstring already named this phase as the one that fills
  it. A second name for one function is the thing the repo's own docstrings warn about
  (`band_for`, `is_thin` — the same ruling as Phase F).
- **MAX_TOKENS is rejected BEFORE the JSON decode, not after it.** A truncated reply almost always
  fails to parse anyway, so the check looks redundant — until the fragment happens to parse and
  happens to satisfy the schema, and a judgement is stamped on the beginning of an answer. It gets
  its own error type, `output_truncated`, not `json_invalid`, because the two send an operator to
  different places: one to the ceiling, one to the template. A test drives a truncated reply whose
  text is a COMPLETE valid object, which is the case that would otherwise pass.
- **The reprompt carries the SAME ceiling as the first call.** Raising it on the second attempt would
  make the second call differ in two ways, and the tail is already the lever: "keep it compact, an
  answer cut off part-way through is rejected exactly as a wrong one is". **The alternative** — a
  larger ceiling on the retry — hides an undersized ceiling behind a paid second call on every
  Arabic note instead of surfacing it.
- **Ceilings are per-task constants beside each task's template and label, not `TenantConfig` fields
  and not one shared table.** `TenantConfig` is the rubric seam — per-tenant and business-changeable
  — and an output ceiling is neither. The number lives next to the thing that knows what the answer
  looks like. **The alternative** — a settings field per task — makes three deployment knobs whose
  wrong value is a 503, and `Settings.llm_max_output_tokens` already exists as the default these
  override.
- **`max_output_tokens` is a REQUIRED keyword on `complete_once` and `call_model`.** The seam's
  `None` (= the configured default) was available and is exactly what nobody revisits; requiring the
  argument means a fourth task cannot be added without someone deciding what its answer costs.
- **The three numbers, and the arithmetic behind them.** The rule is the lead's: size on the longest
  ARABIC answer, never the English one, because truncation is malformed here and an English-sized
  ceiling would spend an Arabic note's one reprompt and then 503 it. Only ONE of the three answers
  carries the note's language back:
  - **vague = 1024.** The only pass with free text in it. Structure ~40 tokens; the clarification
    prompt is capped by the schema at 300 characters, which at ~5 characters an Arabic word and
    ~4 tokens a word (2–3× English's ~1.3) is ~240 tokens; `reasoning` is one or two sentences the
    schema does not cap at all, so allow the same again. ~520 worst case, doubled for a model that
    formats across lines. It coincides with `Settings.llm_max_output_tokens`, which was sized the
    same way for the same reason — this pass is why that default is 1024.
  - **classify = 64.** `{"note_type": "no_contact"}` is 27 ASCII characters from a fixed vocabulary.
    Nothing in it scales with the note, so it does not move with Arabic. The headroom is so that a
    fenced or prefaced reply FITS and is rejected as malformed — a ceiling tight enough to truncate
    it would report the wrong fault.
  - **score = 256.** Five fixed ASCII keys and five whole numbers, ~110 characters, or twice that
    pretty-printed. Also language-invariant.
  Three tests restate the arithmetic so that lowering a constant has to argue with it, including one
  that computes the English-sized ceiling explicitly and asserts we are above it.
- **The log field `model_calls` became `model_passes`.** After this phase the pipeline can no longer
  know the call count — the reprompt is inside `call_model`, which is the point — so a field named
  `model_calls` would be short by up to three. **The alternative:** thread a count back through
  `call_model` → `classify`/`detect_vagueness`/`score_note` → the pipeline, turning three 2-tuples
  into 3-tuples across five call sites and every test that unpacks them, to carry a number only a log
  line reads. `reprompt_issued` already names the pass that needed a second call, so the two lines
  together are a complete picture.
- **The three task test helpers now script the SAME answer twice.** Every "this shape is rejected"
  test is now a claim about a model that does not fix itself, which is what a rejection means after
  the reprompt exists. A valid answer never reaches the second entry, and the `call_count == 1`
  assertions on the happy paths are what keeps that honest — if a valid answer ever earned a
  reprompt, those fail. **The alternative** — passing the bad answer twice at ~40 call sites — is the
  same fact written 40 times.
- **`llm_call.py` gets a 100 floor.** §5 names no floor for this phase. It is the one place model
  output becomes a typed object and now the one place a malformed answer decides its own fate; every
  branch in it is a security branch. It costs one more row in the floors table (see the lead's note
  about double-counted globs) and it is at 100 % today.

**Tree disagreements:**

- **§5 names the function `call_validated`; the tree has `call_model`.** Same signature shape, same
  return type, and `call_model`'s own docstring (written in Phase D) already said Phase G would fill
  it and that "the call sites do not change when it does". Followed the tree — the campaign's rule —
  and did not add a second name.
- **§5 says the reprompt is issued "on `OutputValidationError` (including MAX_TOKENS truncation)".**
  In the tree, a MAX_TOKENS response was NOT an `OutputValidationError` unless its text also failed
  to parse: `complete_once` returned it and `parse_output` looked only at `.text`. Making §5's
  parenthesis true required a rule, so one was added — and it is written as its own error type rather
  than folded into `json_invalid`. Not a disagreement about intent; a gap between the sentence and
  the code, closed in the code.
- **Register item 15 is not in §5's Phase G section at all** — it arrives as the lead's instruction
  this session. It is implemented here because truncation is the failure it prevents and this is the
  phase where truncation becomes malformed. Recorded so a reader of §5 alone is not surprised by it.
  (The register itself is still not in the tree; "register item 15" is a pointer into a document this
  repository does not hold — the same gap session 4 recorded for item 7 and "Appendix B #11".)
- **The concurrent issue order with a reprompt in it is an event-loop property, not a contract.**
  Verified empirically: `classify` (1), `vague` (2), `vague` reprompt (3), `score` (4) — the vague
  coroutine runs to completion before scoring issues its call, because `asyncio.wait_for` on 3.12
  awaits the coroutine inline and `FakeLLM.complete` never suspends. The route test does NOT depend on
  that order to pair answers with consumers: it asserts on the recorded PROMPTS (which template, which
  one carries a tail), so a future interleaving change is a loud, immediately-readable failure rather
  than a confusing validation error. Same approach Phase F took, extended to the reprompt.

**Tests:** 32 added (22 `test_reprompt.py` + 4 `test_assembled_prompt.py` + 4 routes + 2 log-safety),
plus one more case in the template-grep parametrisation, which now sweeps the tail file too — 33 more
collected. 5 existing renamed ("one call" → "two calls", "eight files" → "nine"), and the three task
helpers plus 6 route/unit tests rewritten for two calls. Suite **634 total, 99.13 %** (floor 92). **12 floors met,
one new:** `src/dodeal_ai/units/structured_intelligence/llm_call.py = 100` (actual 100 %). `classify.py`,
`vague.py`, `scoring.py`, `schemas.py`, `state.py`, `config.py` all at 100 %; `pipeline.py` 97.85 %
against its 90 floor, unchanged — its only gap is still the `judgement_completed` branch, unreachable
until Phase H fills in `decide`. mypy: 58 source files, clean. Integration suite re-run separately
(`-m integration`): **7 passed**. Wheel rebuilt and inspected: **nine** prompt files ship, including
`reprompt_tail_v1.txt`.

Closing chain: `pytest` 634 passed / 99.13 %; `ruff check .` all checks passed; `ruff format --check`
**102 files already formatted over `src tests scripts`** (the unscoped run names one untracked file
that is not this phase's — see "For the lead"); `mypy` clean over 58 source files;
`check_coverage_floors` all 12 met.

**For the lead:**

- **The §2.3 "75" is now closed** by your ruling: 60 for `no_contact` in every case, the tree is right,
  no code change. Phase J's ASSUMPTIONS §3.4 entry should read **100 / 80 / 60**.
- **The Arabic ceilings are reasoned, not measured, because there is no corpus in the tree.** The
  numbers come from the schema's own 300-character cap plus a stated 2–3× tokens-per-word model, and
  the tests carry that arithmetic explicitly. **When the corpus lands (Phase I), the honest check is
  to tokenise the longest Arabic notes' expected answers against the real provider's tokeniser and
  confirm 1024 still clears them.** Until then the failure mode is visible rather than silent: an
  undersized ceiling shows up as `output_truncated` in `output_validation_failed`, followed by
  `reprompt_issued`, on the vague label.
- **`model_calls` → `model_passes` is a log-field rename** on `judgement_suppressed` /
  `judgement_completed`. Nothing outside this repo consumes it yet, but if a dashboard has been drafted
  against the old name, this is the moment to change it.
- Phase H still raises `pipeline.py`'s floor to 95 and deletes the `not_implemented` stub; the
  `score=` parameter on `_log_outcome` goes with it.
- **`uv run ruff format --check .` is RED, and the one file it names is not this phase's.** An
  untracked `docs/audit/2026-09-05-sweep.md` appeared mid-session; ruff formats Python code blocks
  inside Markdown, and one block in it (`test_normalise_tenant_label_never_returns_anything_but_none_or_a_valid_label`,
  around line 190) has a blank-line difference. The chain is green over everything else —
  `ruff format --check src tests scripts` → **102 files already formatted**, and
  `--check . --exclude docs` → **106 files already formatted**. The file was not touched, not
  formatted and not staged: it is yours, it is untracked, and reformatting a document I do not own to
  make my own chain report green would be the wrong fix. Either the block gets its blank line or
  `docs/` joins ruff's `extend-exclude` — your call, and it is a one-line change either way.
- `.env.example` is still modified-unstaged in the working tree and was not touched. `AIService.zip`
  and `docs/campaign/` are still untracked and were left alone.

## Phase H — decide, the clarification loop, the rate limit   STATUS: DONE ae62103

**What changed:**

- `src/dodeal_ai/units/structured_intelligence/decide.py` — **new.** `decide(score, analysis, *,
  attempts, rate_count, config, resubmission) -> Decision`, plus `_action` (the total against the
  tenant's two thresholds) and `_withheld` (the four conditions in their fixed order). Pure: no I/O,
  no clock, no model.
- `src/dodeal_ai/units/structured_intelligence/pipeline.py` — the two counter reads now keep their
  return values and carry them to `decide()`; the scored branch builds a real `Judgement` (score **and**
  decision) instead of `Suppressed(not_scorable, not_implemented)`; `model_version` is the **scoring**
  response's model; `_check_one_model_answered` logs `model_version_mismatch` when the three passes did
  not come back from one model; the increments and the reference write run **after** the try block, only
  when `decision.prompt_sent`. `_log_outcome` lost its `score=` parameter and the completed line gained
  `denominator`, `prompt_withheld` and `attempt`.
- `src/dodeal_ai/units/structured_intelligence/schemas.py` — `SuppressedDetail.NOT_IMPLEMENTED`
  **deleted** (three members now), and the docstring that attributed it to `SEAM[STEP3]` corrected — it
  was never the pre-flight's answer, it was the missing `decide()`. `Decision` gained
  `original_note_fingerprint: str | None = None` (register item 33) and a docstring saying no field on it
  can come from a model.
- `src/dodeal_ai/units/structured_intelligence/state.py` — `write_attempt_fingerprint` (SET NX EX) and
  `read_attempt_fingerprint` (GET), `_attempt_fingerprint_key`, and the module docstring's `attempts`
  entry extended to name the reference. No change to `_incr_with_window`, to either counter, or to any
  failure policy.
- `scripts/check_coverage_floors.py` — `decide.py` 100, `pipeline.py` 90 → **95**, and the header list
  gained a `decide.py` line.
- `tests/unit/test_decide.py` — **new, 27 tests.**
- `tests/unit/test_judgement_routes.py` — 15 added (the five full-flow scenarios, the four edges, the
  three item-33 tests, the two version-stamp tests, §2.9's HTTP half); 3 deleted; 1 renamed; 3 rewritten.
- `tests/unit/test_unit_a_state.py` — 7 added for the reference key.
- `tests/unit/test_unit_a_schemas.py` — 1 added (the `not_implemented` grep over `src/`); the
  `SuppressedDetail` vocabulary assertion is now three members.
- `tests/unit/test_reprompt.py` — 1 added (§2.9: a provider failure ON the reprompt).
- `tests/security/test_log_safety.py` — 1 sentinel: the stored reference fingerprint.
- `tests/unit/test_judgement_pipeline.py` — 3 assertions moved from the suppressed stub to the completed
  judgement; the module docstring no longer describes a stub.
- `CAMPAIGN_REPORT.md` — the session 6 Phase 0 block, Phase G's sha backfilled, and this block.

---

### The `Decision` as returned, on both routes (the §2.5 update)

**Primary route** `POST /api/v1/notes/judgements` — scenario 2, the fixture's fair vague note
(55 of 80 → 69):

```json
"decision": {
  "action": "accept_flag_prompt",
  "prompt_sent": true,
  "prompt_withheld": null,
  "attempt": 1,
  "attempts_remaining": 0,
  "original_note_fingerprint": null
}
```

`original_note_fingerprint` is **always** `null` here. On this route, this request either IS the first
prompt or there was none — there is no earlier judgement for the CRM to link to.

**Resubmission route** `POST /api/v1/notes/judgements/resubmission` — scenario 4, the same note edited
after the question:

```json
"decision": {
  "action": "accept_flag_prompt",
  "prompt_sent": false,
  "prompt_withheld": "resubmission",
  "attempt": 1,
  "attempts_remaining": 0,
  "original_note_fingerprint": "7b3da2e3a3e2f86734f6d1586d7e0a3fa6790e942bbbccfd458f41684e6506f8"
}
```

That hex is the SHA-256 of the note **as it was first prompted on** — not the edited text this request
judged. `prompt_sent` is always `false` and `prompt_withheld` always `"resubmission"` on this route;
`attempt` is read and never incremented. The **action is computed exactly as on the primary route**: a
resubmission that scores 85 comes back `accept_silent`, and one that scores 20 comes back
`prompt_clarification` with the question in `analysis` for the CRM to show, unsent.

Field order is the model's declaration order and is stable. `attempt` is the count **after** this
request. `attempts_remaining` is `max(cap − attempt, 0)`.

---

### §2.6 — the item 33 storage choice

**Chosen: a second key, `attempt_fp:{tenant}:{lead_id}:{note_id}`.**

It is the smaller `state.py` change by a wide margin. The two new functions are eleven statements
between them and are built from the two primitives the module already issues — `SET NX EX` (the same
command `reserve_idempotency` uses) and `GET` (the same command both counter reads use). Nothing
existing moves: `_incr_with_window`, `read_attempts`, `increment_attempts` and both failure policies are
byte-identical to Phase B's. **And `FakeOperationalRedis` needed no extension** — it implements exactly
the six commands `state.py` issues, and both new functions issue two of those six.

**The alternative — counter and fingerprint together in one `HSET` — and what it would have cost.**
One key and one TTL for the pair, which is the real thing it buys. Against that: the attempt counter
would have to become `HINCRBY`, which forks `_incr_with_window` — shared today by the rate limit and the
attempt counter, and the one place audit finding M4's `TTL == -1` repair lives. Two counters that stopped
being the same code would be two places for that repair to drift. It also needs `hset` / `hget` /
`hincrby` added to `FakeOperationalRedis`, which §2.6 says counts against "smaller", and a hash's TTL is
on the key rather than the field, so the M4 edge would need re-deriving for a hash. Roughly four times
the diff, in the module the spec calls out as having three deliberately different failure policies.

**The second key's own cost, stated plainly:** the counter and the reference are two writes and two
TTLs, so they can diverge. In practice they are written microseconds apart with the same
`attempt_ttl_seconds`, and the only way to get one without the other is a partial store outage — where
`INCR` fails and `SET` succeeds, or the reverse. In that case the reference reads back while
`read_attempts` returns 0. **That is a divergence from §2.6's literal wording** ("when the attempt counter
… exists in db2, else null") and it is the more truthful answer: we did prompt, and this is the note we
prompted on. Recorded under "For the lead".

Constraints held, each with its test: same TTL (`test_the_reference_lives_beside_the_counter_on_the_same_ttl`) ·
same fail-open policy and **no new bypass code** — `attempt_counter_bypassed` on both sides
(`test_the_reference_fails_open_on_both_sides`) · written only at the moment `prompt_sent` becomes true
(`test_scenario_1…` asserts the key is absent when nothing was asked; `test_scenario_2…` asserts it is
present and equals the fingerprint when it was) · fingerprint only, never note text
(`test_the_reference_is_a_fingerprint_and_never_the_note`) · and the log-safety sentinel
(`test_the_resubmission_reference_never_reaches_a_log_line`).

`SET NX` rather than `SET`: the field is specified as the note "as it was **first** prompted on".
`clarification_cap` is 1, so a second prompt cannot happen today and a plain `SET` would behave
identically — but NX makes the claim true by construction instead of by the cap's current value.

---

**Decisions taken here:**

- **`decide()` answers two questions separately, and the advice ignores every counter.** The action is
  the total against `accept_threshold` / `flag_threshold` and nothing else; only *whether we ask* looks
  at the counters, the route and the presence of a question. **The alternative** — folding "rate limited"
  into the action, e.g. downgrading `prompt_clarification` to `accept_flag_prompt` when we cannot ask —
  would make the CRM's record of a note's quality depend on how many questions its author happened to
  receive that hour. Two identical notes would be judged differently, and neither judgement would say why.
- **The four conditions are four `if`s, not an `all()`.** The order IS the contract, and a chained
  boolean answers "may we ask?" while answering nothing at all about "why not?". **The alternative** costs
  one line and loses the reason the CRM shows a salesperson.
- **`accept_silent` carries `prompt_withheld: null`, never `nothing_to_ask`.** There was no prompt to
  withhold. **The alternative** would tell the CRM we wanted to ask something and could not, which is the
  opposite of what happened — and would make "withheld" useless as an alert, since every good note would
  raise one.
- **The increments are placed after the `try`, not inside it.** `state.py` already swallows `RedisError`,
  so this is belt and braces — but the guarantee "an increment-time store outage does not fail an
  already-judged request" is then structural here, not a promise another module keeps. **The alternative**
  (inside the `try`) is correct today and one refactor away from releasing the idempotency key and
  503-ing a judgement that was already made.
- **The two counter reads are taken once, before the model calls, and carried down.** **The alternative**
  — re-reading them after the three passes, when they are actually needed — would let a concurrent
  request's increment change this judgement's answer halfway through, and would put two more db2 reads on
  the paid path.
- **The resubmission reference is read only in the scored branch of the resubmission route.** It belongs
  to a `Decision`, and a suppressed or failed judgement has none to hang it on — so no thin note, no
  `system_event` and no primary-route request spends a db2 read on it.
- **`decide()`'s signature is the spec's, so the pipeline attaches the reference with `model_copy`.**
  **The alternative** — a seventh parameter — would put a stored value into a function whose whole claim
  is that it is pure and computes from a total, two integers and the config. The reference is a thing the
  CRM links on, not an input to the decision.
- **`model_version` is the scoring pass's model** (ruling §2.2), and a disagreement between the three
  passes is logged rather than merged or refused. **The alternative** — stamping the classifier's, as the
  stub did, or stamping all three — would either attribute the marks to a model that did not produce them
  or change `Versions` into a list, which every downstream comparison would then have to understand.
- **`_log_outcome` reads the score off the judgement rather than beside it.** Two sources for one number
  is how a log line starts disagreeing with the response it describes.
- **`test_a_suppressed_judgement_still_carries_all_four_versions` renamed** to
  `test_a_judgement_carries_all_four_versions`. Its body posts an ordinary request, which is no longer a
  suppressed judgement — the name would have been the exact defect the audit's category G is about.
  `test_a_suppressed_classification_still_stamps_the_model_that_ran` already covers the suppressed case.

**Tree disagreements:**

- **`docs/audit/2026-09-05-sweep.md` cites `test_a_failure_after_reserving_releases_the_key` at
  `:434-442` and the lead's §1.6 repeats it; the tree has it at `:445-455`.** Phase G added four tests
  above it. Same test, deleted per §2.4.
- **§0.4's `uv run ruff format .` and §0.3's "do not touch `docs/audit/2026-09-05-sweep.md`" are in
  direct conflict, and the conflict is live.** `ruff format .` formats that file — it is the one file
  `--check .` is red on. I ran it once, saw it add a blank line at `:190`, and **restored the file
  byte-for-byte**; `ruff format --check .` now reports exactly the Phase 0 baseline (same file, same line,
  same "1 file would be reformatted, 114 files already formatted"). Every subsequent format run was
  `uv run ruff format src tests scripts`. §0.3 wins over §0.4; the file is untouched and unstaged.
- **§2.6's wording ties the reference's existence to the attempt counter's**; with a second key they are
  two entries that can diverge under a partial outage. Recorded above and under "For the lead".
- `model_version_mismatch` is **not** in §2.1's list of audit/log-only codes. Ruling §2.2 directs it, so
  it exists; it is not a bypass code and adds no policy. §2.1's list should gain it in Phase J.

**Tests:** **52 added**, 3 deleted, 1 renamed, 6 rewritten. Suite **683 passed, 7 deselected**,
**99.32 %** total coverage.

*Added* — `test_decide.py` 27 (new file) · `test_judgement_routes.py` 15 · `test_unit_a_state.py` 7 ·
`test_unit_a_schemas.py` 1 · `test_reprompt.py` 1 · `test_log_safety.py` 1.

*Deleted* — `test_a_failure_after_reserving_releases_the_key` (audit S2-2; its body asserted a 200 and a
held reservation) · `test_the_seam_returns_a_not_scorable_suppressed_judgement` · `test_the_score_is_computed_but_not_yet_published`
(both described the stub this phase removed).

*Renamed* — `test_a_suppressed_judgement_still_carries_all_four_versions` → `test_a_judgement_carries_all_four_versions`.

*Rewritten* — `test_resubmission_returns_the_same_shape` and `test_a_judgement_is_logged_without_note_text`
(routes); `test_after_a_release_the_same_request_succeeds`,
`test_vague_and_scoring_are_issued_before_either_returns`,
`test_a_no_contact_note_is_scored_against_the_narrower_rubric` (pipeline);
`test_remaining_vocabularies_are_closed_sets` (schemas).

**Every floor, as the script reports it — all 13 met:**

| Pattern | Floor | Measured |
| --- | --- | --- |
| `core/auth/**` | 95 | 96.00–100.00 |
| `core/tenancy.py` | 100 | 100.00 |
| `core/cost/**` | 95 | 100.00 |
| `core/errors.py` | 95 | 100.00 |
| `core/validation.py` | 100 | 100.00 |
| `core/log_safety.py` | 100 | 100.00 |
| `units/structured_intelligence/**` | 95 | 100.00 (all ten files) |
| `units/…/config.py` | 100 | 100.00 |
| `units/…/state.py` | 95 | 100.00 |
| `units/…/scoring.py` | 100 | 100.00 |
| `units/…/llm_call.py` | 100 | 100.00 |
| **`units/…/pipeline.py`** | **95** (was 90) | 100.00 |
| **`units/…/decide.py`** | **100** (new) | 100.00 |

**The closing chain:**

```
$ uv run pytest
  683 passed, 7 deselected in 7.74s
  Required test coverage of 92.0% reached. Total coverage: 99.32%

$ uv run ruff check .
  All checks passed!

$ uv run ruff format --check .
  unformatted: File would be reformatted
     --> docs\audit\2026-09-05-sweep.md:190:1
  1 file would be reformatted, 114 files already formatted        (exit 1)

$ uv run ruff format --check src tests scripts        (§0.4's fallback)
  104 files already formatted

$ uv run mypy
  Success: no issues found in 59 source files

$ uv run python scripts/check_coverage_floors.py
  All 13 coverage floors met.
```

Block 3 is red on the same one untracked file, at the same line, as it was before this phase started —
see the tree disagreement above.

**For the lead:**

- **§2.6's "when the attempt counter exists" now means "when the reference beside it exists".** With a
  second key they are two entries; a partial db2 outage can leave one without the other, and in that
  case the reference is returned. It is the more truthful answer and it never invents one, but it is not
  the letter of the amendment. Say if you want the read gated on `read_attempts() > 0` instead — one
  `if`, one extra db2 read on the resubmission route.
- **`model_version_mismatch` is a new log-only code**, directed by ruling §2.2 and absent from §2.1's
  list. Phase J's ASSUMPTIONS/README work should add it, or you should tell me to drop the line.
- **Audit S2-6 is untouched per §2.8 and is now adjacent.** `RESUBMIT` still has no gate-2 or gate-4
  assertion anywhere, and this phase gave that route real behaviour of its own (the reference read, the
  withheld prompt) for the first time — so the gap covers more than it did yesterday. Still not H's.
- **Register item 63 (the dangling sibling on the gather failure path) is unchanged and no worse.** The
  gather block was not touched. What H added runs strictly after it.
- **`clarification_cap` is 1, so `attempts_remaining` is only ever 1 or 0** and the `attempt_cap` reason
  fires on the second request for a note. That is the config, not a decision taken here — but it means
  the loop is one question per note, and if Product wanted two the only change is `config.py`.

## Phase I — prompt hardening, adversarial suite, OWASP checkpoint, eval marker   STATUS: DONE 403afa7

Phase I is being taken in pieces at the lead's direction (a divergence from §0.1's one-commit-per-phase,
recorded here so the next session does not read it as drift). The phase is `DONE` only when every piece is.

### Piece I.1 — vendor fake CRM fixtures   STATUS: DONE ba44c5c

**What changed:**

- `tests/fixtures/fake_crm/tenant-a.json` — **new to git** (the lead copied it into the working tree; this
  commit vendors it). 1448 leads, 127 notes across 19 leads, 27 `timeline_events` across 20 leads,
  seed `fake-dodeal-crm-seed-1`, tenant label `tenant-a`.
- `tests/helpers/fake_leads.py` — added `FIXTURE_PATH` and
  `load_fixture_client(path: Path = FIXTURE_PATH) -> FakeLeadsClient`, which parses the corpus through
  `Lead` and `LeadNote` — the same models `LeadsClient` validates real backend JSON with — so the corpus
  is *proven* to match the backend's shape rather than assumed to. Two new `FakeLeadsClient` fields,
  both defaulted and both set only by the loader: `seed` (provenance) and `skipped_invalid_leads`.
  `lead()` and `note()` are untouched; every existing test still hand-builds its own records.
- `tests/unit/test_fake_crm_fixture.py` — **new.** Five tripwires on the corpus.
- No change under `src/`. No existing test's data changed.

**Phase 0 findings (the numbers the tripwires pin):**

- seed `fake-dodeal-crm-seed-1` · 1448 leads · 127 notes.
- **No note has `"note": null`.** Review finding F1's hazard is not present in this fixture, so no skip
  path, no `skipped_null_notes` field and no null-count assertion were added — per instruction. The
  guard is implicit instead: `LeadNote` requires a non-null `note`, so a regenerated corpus that
  reintroduced one would fail at load, inside `test_every_loaded_note_is_a_lead_note`.
- Every one of the 127 notes carries all five of `id`, `note`, `author`, `author_id`, `createdAt`.
  `author` is null on 11 of them, which `LeadNote` allows and documents (deleted author account).
- Note ids are unique across the corpus and each lead's notes are already newest-first, so the loader
  preserves file order and never sorts.

**Decisions taken here:**

- **Lead 1661 does not validate, and the loader skips it rather than repairing it.** It carries
  `bookedAmount: "1,250,000"` — a formatted string where `Lead.bookedAmount` is `float | None`. It is
  the only such record in 1448, and it has no notes, so the 127-note corpus is untouched by the skip.
  The two alternatives both cost more than they save: *coercing* the string would invent a parse rule
  for a field `schemas/lead.py` explicitly calls UNCONFIRMED (and "1,250" is a thousands separator in
  one locale and a decimal comma in another — the fixture cannot tell us which the backend means), and
  *relaxing* `Lead` is a change under `src/`, which this piece is forbidden. So: skip, count, pin the
  count at exactly 1. A second bad lead fails `test_skipped_invalid_lead_count_is_pinned` instead of
  disappearing into a `try`.
- **`timeline_events` is not loaded into `notes`.** It is a separate top-level key (27 records, same
  five fields), and it is not what `GET /leads/{id}/notes` returns. Folding it in would grow the corpus
  the campaign counts from 127 to 154 and would feed machine timeline text to the note pipeline as if a
  salesperson had written it. Cost of the alternative: the `system_event` path loses its natural corpus
  and Phase I.2+ must build one — cheap, and correct, versus a silent 21 % corpus inflation.
- **A skipped lead does not cost its notes.** Leads and notes live under different top-level keys;
  dropping a lead's notes over an unrelated money field would shrink the corpus for no reason. Moot
  today (1661 has none), stated so the next reader does not have to re-derive it.
- **The seed lives on the client, not in a module constant.** A constant would restate the seed rather
  than read it, which is not a tripwire. `client.seed` is read from the file every load.

**Tree disagreements:**

- **`Lead.bookedAmount` vs the corpus** — the disagreement above. Per §0's "the code is the fact", the
  tree wins: the schema is unchanged and the fixture record is skipped and counted. This is the first
  hard evidence in the repo about what that field can actually contain, and it needs a ruling — see
  **For the lead**.
- **The working tree was not clean at Phase 0.** `.env.example` is still modified-unstaged and was not
  touched (§0.8). `AIService.zip` and `docs/audit/2026-09-05-sweep.md` remain untracked and were left
  alone. `docs/campaign/UNIT_A_PROJECT1_CAMPAIGN_PROMPT.md` is the lead's own spec drop and is the
  lead's to commit, not a session's (§0.13). Staging was by explicit path, so none of it was swept in.
- **Phase H's block read `STATUS: DONE <sha>`** and was backfilled to `ae62103` as this piece's first
  edit, per the report's sha convention.

**Tests:** 5 added; suite 688 total, 99.32 % coverage; all 13 per-file floors met. No new floors — this
piece adds nothing under `src/`, and `scripts/check_coverage_floors.py` only governs `src/`.

**For the lead:**

- **What is `bookedAmount`'s real type?** The corpus says a lead can carry `"1,250,000"`. If the backend
  really can send that, `Lead` is wrong today and the real `LeadsClient` will raise on that lead in
  production — a one-line validator, but a change under `src/` and not this piece's. If instead the
  fixture generator is wrong, regenerate lead 1661 as an int or null and
  `SKIPPED_INVALID_LEADS` drops to 0 in the same commit. Either way this is a real question, not a
  fixture blemish; I have deliberately not answered it.
- **Are `timeline_events` meant to be reachable through `get_lead_notes`?** If the `system_event`
  short-circuit is supposed to be exercised against the corpus, something has to serve those 27
  records. Say the word and I.2 adds a separate accessor — I will not fold them into `notes`.
- **The lead count a downstream eval quotes is 1447, not 1448.** One is in the file and not in the
  client. Worth knowing before anyone writes "the 1448-lead corpus" into a document.

### Piece I.2 — FakeLLM prompt-directed scripting   STATUS: DONE 335abf4

**What changed:**

- `tests/helpers/fake_llm.py` — added `FakeLLM.script_for(template_name, *responses)`, which resolves the
  template's `stable` text through `build_prompt` (the same loader the pipeline assembles with, never a
  test re-reading the file) and queues the responses under that string. Added `_TemplateQueue` (the
  queued items plus the template NAME, so an exhaustion message can blame something a reader
  recognises) and `FakeLLM._by_template`. `complete()` now delegates the choice of answer to
  `_next_for(prompt)`; everything else in it — the call record, the `hold_after` wait, raising a
  scripted exception — is unchanged and in the same order.
- `tests/helpers/test_fake_llm.py` — 6 tests added (5 functions, one parametrized over both gather orders).
- Untouched, as required: the constructor, `script`, `rescript`, `hold_after`, `calls`, `prompts`,
  `call_count`, and the positional exhaustion message. No existing test changed. No change under `src/`.

**The precedence rule:** if a template queue **exists** for `prompt.stable`, the answer comes from it;
otherwise the positional script serves the call, exactly as before. *Exists* is not *non-empty* — an
exhausted template queue raises `FakeLLMExhausted` naming the template rather than falling through to the
positional script, because a silent fall-through would answer a scoring call with whatever the test had
lined up for something else. A reprompt stays on its queue for free: `with_tail` carries `stable` across
untouched and changes only the tail, so the second call for a pass pops the next item.

**Why it was needed (Phase 0 finding):** positional scripting survives the vague/score `asyncio.gather`
today only because gather wraps its arguments into tasks in argument order and steps them FIFO, and
nothing between the pipeline and the fake suspends — `build_prompt` is sync, `asyncio.wait_for` with a
positive timeout awaits the coroutine directly rather than creating a task, and `FakeLLM.complete` never
awaits anything unless `hold_after` is set. So `detect_vagueness`, gather's first argument, runs straight
into `complete()` and takes script slot 1. That is a scheduling accident the pipeline never promised, and
Piece I.7 cannot script 127 notes against it. Verified both ways before writing the tests:
`gather(vague, score)` arrives `['vague', 'score']` and `gather(score, vague)` arrives `['score', 'vague']`,
so the parametrized test is not a no-op.

**Decisions taken here:**

- **`script_for` appends on a second call for the same template; it never replaces.** Two notes' worth of
  answers for one template is precisely the I.7 corpus case, and a second call that silently discarded the
  first would lose one. The alternative — replace, mirroring `rescript` — reads tidier in a two-line test
  and quietly breaks the case this piece exists to serve.
- **Keyed on `stable`, carrying the name alongside.** `stable` is what `complete()` can actually see, but
  a failing test needs to read `structured_intelligence/score_v1.txt`, not 40 lines of template. Reverse-
  looking-up the name from the text at failure time would have worked and would have been one more thing
  to be wrong.
- **An unknown template name raises `PromptError` inside `script_for`,** at the line that named it, rather
  than never matching a call and presenting as a pipeline bug. This is free — it falls out of resolving
  through `build_prompt` — and it is the reason to resolve through the loader rather than a literal.
- **The precedence check lives in `_next_for`, not inline in `complete()`.** `complete()` keeps its
  record-then-hold-then-answer shape unchanged, which is what makes "no existing test changes" checkable
  by reading rather than by running.

**Tree disagreements:** none. `.env.example`, `AIService.zip`, `docs/audit/` and `docs/campaign/` remain
modified/untracked and were left alone; staging was by explicit path.

**Tests:** 6 added; suite **694** total, **99.32 %** coverage; all 13 per-file floors met. No new floors —
this piece adds nothing under `src/`, and `scripts/check_coverage_floors.py` only governs `src/`.

**For the lead:**

- **The gather-order accident is now covered but not removed.** Every test still using two positional
  responses across that gather passes for the reason described above, not because the order is
  guaranteed. Those are not wrong today and I did not touch them (this piece may not change an existing
  test's expectations) — but if you want them converted to `script_for`, that is its own piece.

### Piece I.3 — reprompt tail without the previous-answer fiction   STATUS: DONE 86a0b3c

**What changed:**

- `src/dodeal_ai/prompts/structured_intelligence/reprompt_tail_v1.txt` — rewritten in place. **No version
  bump:** no judgement has ever been produced by this file, so there is no past output whose provenance a
  `_v2` would preserve. This is the only file under `src/` this piece touches.
- `tests/unit/test_reprompt.py` — 1 test added, `test_the_tail_does_not_pretend_there_was_a_previous_answer`.
- No code change. `llm_call.py` is untouched, there is still exactly one tail, and no existing test's
  expectations changed.

**The opening line, before and after:**

- **Was:** `YOUR PREVIOUS ANSWER WAS REJECTED. It was not the object asked for above.`
- **Now:** `Return exactly the JSON object described above, and NOTHING else. These rules govern the FORM
  of the answer only:`

**Why it was wrong:** every model call is a fresh prompt carrying no history. The model has not seen an
earlier answer, has not been told one was turned down, and has read nothing before the prompt in its
hands. The old tail asserted all three — a previous answer, its rejection, and a data section "you have
already read" — and then asked the model to *reconsider* something it never produced. A model has no
honest way to comply with that, and what it does instead is unpredictable: the likeliest failure is that
it invents the answer it is supposed to be revising. The three fictional paragraphs are gone; the form
rules they preceded are unchanged, bullet for bullet.

**Kept, deliberately:** the whole bullet list (no prose, no fence, every field asked for and none other,
value kinds, fixed-list values spelled exactly), the compactness sentence, and
`This is the second and last attempt. There is no third.` as the final line. The tail still names no
field, component, note type, weight, threshold or band — it says nothing a per-task template does not
already say, which is why one file can serve all three passes.

**Decisions taken here:**

- **The compactness line was reworded, not kept verbatim.** It read "...is **rejected** exactly as a wrong
  one is", and `rejected` is one of the four words the new test pins out. Meaning preserved exactly —
  "An answer cut off part-way through **fails** exactly as a wrong one does, so say what is asked and
  stop." The alternative, keeping the word and dropping it from the test's list, would have left the tail
  free to drift back toward the language of rejection one edit at a time.
- **The test pins four words, not a whole-file hash.** A hash would fail on every wording improvement and
  teach the next person to update it without reading. The four words are the ones that carried the
  fiction, so the test fails only when the fiction returns.
- **`This is the second and last attempt` stays, on instruction.** Noting for the record that it is the
  one sentence left that tells the model something about a history it cannot see — it is a true statement
  about the call budget rather than a claim about the model's own past output, and it was explicitly
  required to keep, so it stayed. See **For the lead** if you want it revisited.

**Tests:** 1 added; suite **695** total, **99.32 %** coverage; all 13 per-file floors met. No new floors.
`tests/unit/test_reprompt.py` re-run on its own before the new test was written: **23/23 passing**,
including `test_the_tail_is_the_versioned_file_verbatim` and
`test_the_tail_never_names_what_was_wrong_with_the_answer`.

**Tree disagreements:** none. `.env.example`, `AIService.zip`, `docs/audit/` and `docs/campaign/` remain
modified/untracked and were left alone; staging was by explicit path.

**For the lead:**

- **Review finding F4 — one tail serves both form failures and content failures, where a second tail
  selected by error class would say something more useful — is DEFERRED to the post-campaign batch. It is
  explicitly not this piece,** which was forbidden from adding a second tail. This rewrite does not close
  F4 and does not make it harder: the file is now purely a form instruction, which is the natural first
  half of any later split.
- **`This is the second and last attempt. There is no third.` is the last remaining sentence about a past
  the model cannot observe.** It is true of the system rather than of the model, and it is what stops a
  model from padding for a third try — but if you want the tail to make no claim about attempt ordering
  at all, that is a one-line change and its own decision.

### Piece I.4 — relative times and closures in the templates   STATUS: DONE d0c9acf

**What changed:**

- The six `vague_*_v1.txt` — one sentence added to the `next_step_with_date` entry, identical in all six.
- `score_v1.txt` — the same sentence added under `next_step_date`, plus the closure sentence.
- `tests/unit/test_vague.py` — 1 test, parametrised over the six scored types (6 cases).
- `tests/unit/test_scoring.py` — 1 test.
- Nothing else under `src/`. No code change.

**The two sentences, verbatim** (they wrap differently per file; these are the words):

> A relative time anchored to when the note was written also counts as a date — for example "tomorrow",
> "after 2 hrs", "next Tuesday", "end of the week".

> Full marks also for an explicit closure with a stated reason — the deal closed, the client bought
> elsewhere or withdrew, the lead was dropped — because nothing follows and the note says so.

**Why:** the corpus writes "cb tmrw" far more often than a calendar date, and a template that accepted only
a date or a named day would report `next_step_with_date` missing on notes that state exactly when the next
step is. The closure sentence aligns `score_v1.txt` with MASTER_SPEC §2.4 and with `vague_won_lost_v1.txt`,
which already accepted an explicit closure — until now the two passes could disagree about the same
finished deal, vague detection calling it complete while scoring marked it down for a follow-up it should
never have had.

**Decisions taken here:**

- **The sentence went into the `next_step_with_date` fixed-list entry, not the "What a useful one contains"
  bullet.** The phrase quoted in the instruction appears in both places in four of the six templates — but
  `vague_no_contact_v1.txt` and `vague_won_lost_v1.txt` have no matching "useful one contains" bullet, so
  the fixed-list entry is the only location common to all six, and "same sentence, same words, in all six"
  is only satisfiable there. It is also the definitional one: it is what the model reports against.
- **The tests compare with line wrapping collapsed.** The words are identical everywhere; the wrapping is
  not, because the two files indent differently. Pinning six exact line breaks would fail on the next
  reflow and teach the next person to re-paste rather than read.

**Tree disagreements:** none. `AIService.zip` is no longer in the working tree — removed outside this
session; it was untracked and was never staged here either way. `.env.example`, `docs/audit/` and
`docs/campaign/` remain modified/untracked and were left alone.

**Tests:** 7 added (6 parametrised cases + 1); suite **702** total, **99.32 %** coverage; all 13 per-file
floors met. No new floors. `test_scoring.py`, `test_vague.py` and `test_classification.py` were run on
their own after the template edits and before the new tests: **179/179 passing**, unchanged.

**For the lead:** empty.

### Piece I.5 — few-shot examples   STATUS: DONE f17da28

**What changed:**

- `classify_v1.txt` — **5 examples**.
- `vague_no_contact_v1.txt`, `vague_callback_v1.txt`, `vague_discovery_v1.txt`, `vague_viewing_v1.txt`,
  `vague_negotiation_v1.txt`, `vague_won_lost_v1.txt` — **3 examples each** (18 in total).
- Not touched, as required: `score_v1.txt` and `reprompt_tail_v1.txt`.
- `tests/unit/test_scoring.py` — 5 new test functions covering all nine templates (35 cases with the
  parametrisation).
- No code change. Nothing else under `src/`.

**Placement, in every one of the seven:** after the rules, before the `Return ONLY this JSON object` line.
Opened with "EXAMPLES. These are illustrations of the expected answer, nothing more — the note you must
judge/classify is only the one in the CALLER DATA section below." and closed with "END OF EXAMPLES —
everything above is illustration; the note to judge/classify is the one in the CALLER DATA section below."

**Coverage:** every example is invented — English-dominant with Egyptian and Levantine Arabic, mixed
script, shorthand (`no ans`, `sent wp`, `cb tmrw`, `f/u`, `2x`), deliberate misspellings (`intersted`,
`viewng`, `negotiaton`, `droped`), lengths from one line to four. `classify_v1.txt` covers `no_contact`,
`discovery`, `negotiation`, `system_event` and `unclassifiable`, with the discovery example mixed-language
and the negotiation example exercising the furthest-progress precedence rule. Each vague template carries
one vague example with an **Arabic** clarification question, one non-vague example, and one shorthand
example. Invented names (Hala, Tarek, Nour, Sayed, Dalia) and invented developments (Zahra Gardens, Cedar
Walk) were checked against `tenant-a.json` and appear nowhere in it; no digit run of 8 or more anywhere.

**Decisions taken here:**

- **"One clear case per type across the set" was read as across the seven templates, not within
  `classify_v1.txt` alone.** Eight distinct answers (seven types plus `unclassifiable`) cannot fit in the
  "three to five examples per template" cap, so the two instructions are only jointly satisfiable on this
  reading — and it is the natural one: each vague template is type-specific, so the six of them supply a
  clear case for each of the six scored types, and classify supplies `system_event`, `unclassifiable` and
  the mixed-language note. The conservative choice was to honour the stated cap rather than exceed it;
  raising `classify_v1.txt` to eight examples is a one-file change if you want per-type coverage there
  too. **Recorded for the lead below.**
- **Three examples per vague template, not five.** Every example is text sent on every request, and the
  three chosen (Arabic-question vague, non-vague, shorthand vague) are the three distinct behaviours the
  template has to demonstrate. A fourth would repeat one of them at real token cost.
- **`score_v1.txt` gets no examples, and the test pins that.** Its examples would have to be marks, and a
  mark in a template is the arithmetic leaking into the prompt — the thing
  `test_no_template_in_the_set_names_the_arithmetic` exists to prevent.

**Tests added (5 functions, 35 parametrised cases):** no template carries `dodealcrm.com`, `DODEAL_`,
`tenant-a` or `tenant-b`; no template carries a run of 8+ digits; no template contains `_DATA_START` or
`_DATA_END`; each of the seven judging templates has an `EXAMPLES.` section and an `END OF EXAMPLES` line,
before the return line; and the set of example-carrying templates is exactly those seven. The existing
`test_no_template_in_the_set_names_the_arithmetic` (no weight, threshold, band, total) is untouched and
still passes over all nine files.

**Stable-identity confirmed:** `test_the_second_prompt_differs_only_in_the_tail` and
`test_the_type_chooses_the_template_that_is_sent` re-run on their own and pass —
examples live in the stable half and do not vary per note. `test_vague.py` and `test_classification.py`
together: 118/118.

**Tree disagreements:** none.

**Tests:** 35 added; suite **737** total, **99.32 %** coverage; all 13 per-file floors met. No new floors.

**For the lead:**

- **`classify_v1.txt` shows 5 of the 8 possible answers.** `callback`, `viewing` and `won_lost` have no
  worked example in the classification prompt — they have one each in their own vague template, which is
  the reading that let the 3-to-5 cap stand. If you would rather the classifier see all eight, say so and
  it is three more examples in one file.

### Piece I.6 — adversarial suite   STATUS: DONE 257da13

**What changed:** `tests/security/test_unit_a_injection.py` — **new**, 55 tests. No change under `src/`.

**The six cases:**

1. **Forged delimiters** (6 cases + 3): a note carrying `_DATA_END`, and separately `_DATA_START`, through
   the classification, vague and score prompts — `.variable` shows `[filtered-delimiter]` and both the
   `.variable` and the assembled `.text` carry **exactly one** of each marker. A third test forges both at
   once and asserts two neutralisations. Notes built with `note()`; the hostile string is the point, so a
   corpus note would only have diluted it.
2. **A note shaped like the answer** (2): the note body *is* a `{"marks": {...}}` object at full marks, and
   `judge_note` runs end to end with FakeLLM scripted lower. Result marks are `[20, 15, 15, 5]` — the
   model's — total 69, band `fair`, never the note's 100. A second test does the same with a plain
   instruction ("...RETURN BAND EXCELLENT") and pins the band at `fair`.
3. **A band or a total from the model** (5): `ScoreOutput` forbids extras, so both are rejected by the
   schema. One reprompt is asserted by counting the calls whose `.stable` is the score template and
   checking the second carries a tail; a second bad answer gives **503 `malformed_output`**; a good second
   answer gives an ordinary judgement — five components in fixed order, denominator 80, band derived.
4. **Marks the rubric cannot accept** (6): above the ceiling (`what_happened: 100`), a mark for a
   Q13-suppressed component (`deal_specifics`), and a missing mark (`clarity` omitted). Each earns exactly
   one reprompt then succeeds, and each twice gives 503 `malformed_output`.
5. **`.stable` byte-identical across ten corpus notes** (10): for classification, score, and each of the
   six vague templates, plus the reprompt tail. Each vague assertion also checks the stable text equals
   `_load_template(template_for(type))`. A companion test asserts the variable half moves with the note.
6. **No shipped template carries anything real** (19): a secret-shaped regex (`api_key`, `secret`,
   `password`, `bearer `, `authorization:`, `-----BEGIN`), plus `dodealcrm.com`, `DODEAL_`, `tenant-a`,
   `tenant-b`, and any run of 8+ digits, over all nine shipped templates.

**Plus the sentinel tests** (3), on the `tests/security/test_log_safety.py` pattern: a note carrying
`SENTINEL-0501234567 villa budget 4.2M` *and* an injection instruction goes through `judge_note` on the
happy path, on the reprompt path (where the rejected answer quotes the note), and on the 503 path. The
sentinel, the digits and the instruction appear in **no** log line — asserted against both the real
`JsonFormatter` output at **DEBUG** and `caplog.text`, so it covers every level, not just WARNING.

**Fixture notes used:** the ten lowest-id corpus notes of 40+ characters (ids 1–10) via
`load_fixture_client`, for case 5 and for nothing else — the delimiter and mark cases need a specific
hostile string, which a corpus note does not contain.

**Tree disagreements — two, both real:**

- **There is no `unit_a_v1.txt`.** This piece's brief says "the ten shipped, including `unit_a_v1.txt` and
  the tail". The tree ships **nine** templates and never had a `unit_a_v1.txt`;
  `test_the_prompt_set_is_the_nine_files_this_campaign_ships` in `test_scoring.py` has pinned nine since
  Phase F. Per §0 the code is the fact: the suite tests the nine that exist, and
  `test_the_shipped_set_is_the_nine_files_that_exist` asserts the absence explicitly with a comment
  pointing here.
- **The corpus reuses note bodies: 127 notes carry only 57 distinct texts.** Notes 4 and 10 are the same
  sentence. Found while writing case 5, which initially asserted ten distinct data sections and got nine.
  The test now asserts the variable half tracks the note **text**. **This matters beyond this piece** —
  see For the lead.

**Decisions taken here:**

- **Case 1 asserts delimiter COUNTS, never absence.** `.variable` legitimately contains both markers — it
  *is* the delimited section. The first draft asserted the forged string was absent from `.variable` and
  failed correctly against the genuine wrapper. Counting is the claim that actually matters: one pair, the
  one `build_prompt` wrote.
- **The reprompt is counted by filtering prompts on the score template's stable text,** not by
  `call_count`. Three passes run per judgement and two are gathered, so a bare call count cannot say
  *which* pass reprompted.
- **Sentinel assertions run at DEBUG against two capture paths.** The brief says "no log line, at any
  level"; `caplog` alone defaults higher and the `JsonFormatter` capture alone would miss records the
  formatter never sees.

**Tests:** 55 added; suite **792** total, **99.32 %** coverage; all 13 per-file floors met. No new floors.

**For the lead:**

- **The 127-note corpus is 57 distinct notes.** 36 texts are reused across 106 of the 127. For a structural
  eval (I.7) that is harmless — every note still goes through the pipeline. For any *quality* eval it is
  not: a per-note pass rate over this corpus is really a per-note-text rate with some texts weighted 4x.
  Worth knowing before a number from it is quoted. Regenerating with distinct bodies would change the
  counts pinned in `test_fake_crm_fixture.py`, which is exactly the tripwire working.
- **`unit_a_v1.txt` does not exist.** If a tenth template was intended (a shared preamble, say), it was
  never written and nothing references it. Say if it should be.

### Piece I.7 — OWASP note and eval skeleton   STATUS: DONE 403afa7

**What changed:**

- `docs/security/owasp-llm-unit-a.md` — **new.** One paragraph each for LLM01, 02, 05, 07 and 10, each
  naming the control by file and function and the test that proves it; a table of the five that do not
  apply and why; a standing-caveats section.
- `README.md` — linked from the **Security model** section and added to the documentation index.
- `pyproject.toml` — `eval` marker registered beside `integration`. The structural eval runs in the
  default suite (`addopts` deselects only `integration`); only the quality eval is skipped.
- `tests/eval/test_structural_eval.py` — **new**, 4 tests.
- `tests/eval/test_quality_eval.py` — **new**, 1 skipped placeholder. No provider code.
- `tests/helpers/fake_leads.py` — added `load_fixture_timeline_events(path)`.
- No `tests/eval/__init__.py`: nothing under `tests/` has one, and `pythonpath = ["."]` already makes
  `tests.helpers` importable. Adding one here alone would have been the odd file out.
- No change under `src/`.

**The structural eval:** all 127 corpus notes through `judge_note` with `FakeLLM` scripted per template
(classify → `discovery`, the discovery vague template, the score template, each queued 127 times). Every
note yields either a scored judgement (score and decision present, `suppressed` null) or a suppression
(score and decision null, `suppressed` present) — never a mixture, never an exception, and `versions` on
both shapes. **9 notes suppress as `note_too_short`** and that number is pinned; the other 118 score. A
second test re-runs just those 9 against a **completely empty** `FakeLLM`, so any model call at all would
raise, and asserts `call_count == 0` and an empty operational store — thin evidence reserves nothing and
spends nothing. A fourth test states the claim on its own, collecting every exception across the corpus
and asserting the list is empty, so a failure names it rather than reporting a shape mismatch.

**The timeline half:** all 27 `timeline_events` through the same path with classify scripted to
`system_event`. Every one comes back `not_scorable` / `system_event`, and the test asserts that **every
call the model received was a classification call** — the vague and score queues were deliberately filled
and never popped, so a stray call would have shown up as a prompt that is not a classify prompt.

**Decisions taken here:**

- **`load_fixture_timeline_events` returns a mapping, not a client.** Handing back a `FakeLeadsClient`
  with timeline events sitting in the `notes` slot would be exactly the conflation Piece I.1 refused. It
  validates them through `LeadNote` for the same reason the note loader does.
- **The `eval` marker is registered but nothing is deselected by it.** The brief says the structural eval
  runs in CI; the marker exists so the eval suite can be *selected* (`-m eval`) and so the quality
  placeholder has somewhere to hang. `addopts` was not touched.
- **The wheel check was run** because `pyproject.toml` changed (§0.2): `uv build` then
  `scripts/verify_wheel.py` on the built wheel — **`wheel import check: OK`**. Only a pytest marker moved,
  so packaging was never at risk, but the rule says whenever, not whenever it looks risky.
- **Every test name cited in the OWASP note was verified to exist** by matching the document's
  `test_*` references against `def test_*` across `tests/`. One was wrong on the first pass
  (`test_no_shipped_template_contains_the_caller_data_delimiters` for
  `test_no_template_contains_the_caller_data_delimiters`) and was corrected. A security note whose
  evidence column points at tests that do not exist is worse than no note.

**Tree disagreements:** none new.

**Tests:** 5 added (4 + 1 skipped); suite **796** passing + 1 skipped, **99.32 %** coverage; all 13
per-file floors met. No new floors.

**For the lead:**

- **The positional-gather conversion was NOT done, per the "if and only if" condition** — this piece
  touched `tests/helpers/fake_leads.py`, `tests/eval/`, `pyproject.toml`, `README.md` and
  `docs/security/`, and none of the affected test files. The list, for whenever you want it:
  **`tests/unit/test_judgement_pipeline.py`** (`_happy_path()` at line 97, used by the `llm` fixture and
  by `test_the_happy_path_costs_three_calls`) and **`tests/unit/test_judgement_routes.py`**
  (`_happy_path()` at line 103, used by the `llm` fixture and by roughly ten call sites including the
  `rescript` ones). Both build `[classified, vague, score]` positionally across the gather. They pass
  today for the scheduling reason recorded in Piece I.2, not a guaranteed one.

### Phase I — summary

**What changed, across the seven pieces:** the vendored 127-note corpus and its loader (I.1); template-
directed scripting in `FakeLLM` (I.2); the reprompt tail rewritten without its previous-answer fiction
(I.3); relative times and explicit closures accepted by the rubric templates (I.4); few-shot examples in
the seven judging templates (I.5); a 55-test adversarial suite (I.6); the OWASP checkpoint and the eval
skeleton (I.7). Under `src/`, this phase changed **prompt text only** — eight `.txt` files. No Python
under `src/` was touched in any of the seven pieces.

**Decisions worth carrying forward:** vendor data the schemas reject is skipped, counted and pinned, never
repaired (I.1); an existing-but-empty template queue raises rather than falling through to the positional
script (I.2); the tail is a form instruction and says nothing about a past the model cannot see (I.3);
"one clear case per type" is satisfied across the seven templates rather than within `classify_v1.txt`,
which is what let the 3–5 example cap stand (I.5); delimiter claims are about counts, never absence (I.6).

**Tree disagreements found in this phase:** `Lead.bookedAmount` versus the corpus (I.1, open — see below);
no `unit_a_v1.txt` exists, the shipped set is nine templates (I.6); the corpus reuses note bodies, 127
notes carrying 57 distinct texts (I.6).

**Tests:** 109 added across the phase (687 → 796 passing, plus 1 skipped). Coverage **99.32 %**, unchanged
— this phase added no `src/` statements to cover. All 13 per-file floors met; no new floors.

**Suite at the end of Phase I:** 796 passing, 1 skipped, 7 deselected, 99.32 % coverage.

**For the lead, phase-level — four open items:**

1. **`bookedAmount` may be a string.** Lead 1661 carries `"1,250,000"`. Needs confirming against the real
   export; if the backend really sends that, `Lead` is wrong today and the real `LeadsClient` will raise
   on that lead in production.
2. **The corpus is 57 distinct notes, not 127.** Fine for a structural eval, misleading for any quality
   number.
3. **Two test files still script the gather positionally.** Named in I.7 above.
4. **Review finding F4 remains deferred** to the post-campaign batch: one reprompt tail serves both form
   and content failures.

## Phase J — the ledger commit   STATUS: DONE d93d936

> **Resume, 10 September.** This phase stopped red on 9 September and was resumed the next day. The
> BLOCKED report below is kept verbatim, because it is the record of what was wrong and one of its
> conclusions turned out to be false. **Read "Resume, 10 September" at the end of this block for what was
> actually fixed** — including two errors of mine that the BLOCKED report did not catch and in one case
> asserted the opposite of.

### The BLOCKED report, 9 September — kept as written

**Nothing is committed. The working tree carries all of Phase J's work, uncommitted and unstaged**, exactly
as the stopping chain left it. The next session resumes here — do not redo phases A–I.

### What failed

`tests/unit/test_startup.py::test_backend_keys_missing_logs_error_when_map_is_empty`.

```
1 failed, 802 passed, 1 skipped, 7 deselected in 10.48s
E       AssertionError: assert 'backend_keys_missing' in ''
E        +  where '' = <LogCaptureFixture>.text
---------------------------- Captured stdout call -----------------------------
{"level":"ERROR","logger":"dodeal_ai.startup","message":"backend_keys_missing count=0",
 "timestamp":"2026-09-09T21:51:13.929408+00:00"}
```

The other three blocks were not reached. `ruff check`, `ruff format --check` and `mypy` are unrun for this
phase; the last four-block-green state is head `403afa7` (Piece I.7).

### Why it failed, and why it is not a regression

The failing test is the one the §6.9 one-liner was always going to touch, and **the fix is working as
intended** — read the captured stdout above: the line now goes out as a properly formatted JSON object,
which is the entire point of moving it. Before the move it was emitted *before* `configure_logging()` and
went out through whatever handler `logging` happened to have — unformatted, and invisible to a collector
that parses only the JSON lines every other startup event uses.

The mechanism of the failure: `core/logging_config.py` line 95 does `root.handlers = [handler]` — it
**replaces** the root logger's handlers rather than appending. `caplog` works by installing its own handler
on the root. So with the log call now *after* `configure_logging()`, caplog's handler has already been
evicted by the time the line is emitted, and `caplog.text` is empty even though the record was emitted and
formatted correctly.

So: the production behaviour is better than it was, and the test is asserting through a channel the fix
deliberately closed. It is an invalidated test expectation, not a broken feature.

### Not fixed forward, deliberately

The unattended rules for this run are explicit: *"If the chain is red, do not fix forward, do not amend, do
not skip. Stop, leave the working tree as it is."* Changing a test's expectation is a judgement about what
the correct behaviour is, and that is exactly the kind of call the stop rule exists to keep out of an
unattended run — the more so since "change an existing test's expectations" was on the Never list for Piece
I.2. So the tree is left red and untouched.

**The fix, when someone decides to take it** (one line, in the test, not in `src/`): assert against the
formatter's real output rather than through `caplog`. The pattern already exists in this repo — the
`json_capture` fixture in `tests/unit/test_judgement_pipeline.py` and the `log_capture` fixture in
`tests/security/test_unit_a_injection.py` both attach a `StreamHandler(JsonFormatter())` to the `dodeal_ai`
logger and read the stream, which survives `configure_logging()` because it is attached to `dodeal_ai`
rather than to the root. `capsys` would also work, since the line reaches stdout. The companion test
`test_backend_keys_missing_not_logged_when_map_is_non_empty` asserts an **absence** and still passes, but it
now passes vacuously for the same reason — it should move to the same capture channel in the same edit, or
it is no longer testing anything.

**The alternative — reverting the §6.9 move — is the wrong fix** and is recorded here so nobody takes it by
reflex: it would restore a green suite by putting the loudest line in the file back on the channel least
likely to be read.

### What is in the working tree, uncommitted

- `README.md` — a new **Unit A — Project 1** section: the five provisional answers as a table (assumed / if
  wrong / grep marker), the bold **no real provider before step 3 lands** note with the `SEAM[STEP3]` line,
  the full route contract (both shapes, `versions`, the `/resubmission` rules,
  `original_note_fingerprint`, `prompt_withheld` being `null` on `accept_silent` as well as on a sent
  prompt, and rate-limit-never-429), and the `X-Idempotency-Key` decision (not required; the content
  fingerprint is the idempotency; a header stays open for joint design with the CRM). Plus a TOC entry and
  **two stale rows corrected** — see Tree disagreements.
- `ASSUMPTIONS.md` — §3.4 gains the campaign's decided rules as a second table (attempt key, fingerprint
  definition, reserve/release, `prompt_withheld`, rate-limit-never-429, thin-evidence thresholds, the four
  denominators including why **75 is unreachable**, band derivation, relative times, explicit closures);
  marker entries with correction paths for Q1 (§5.1), Q6 (§4.5), Q7 (§4.3), Q8 (**new §5.10**) and Q13
  (§5.2); **new §8.11** "Unit A Project 1 — built against FakeLLM and the fake CRM, nothing `[V]`"; §13
  gains `redis_operational_url` and the note that `/ready` does not yet report db2 (step 3).
- `docs/STATUS.md` — build-position rows for phases A–J with shas; the stale "221 tests, 29 Aug" position
  replaced with the numbers at head; the two §2 debts (**error taxonomy** → DONE phase C, **`TenantConfig`
  seam** → DONE phase A) struck through with what actually landed; the read-after-write bounded re-read
  added as a step-4 candidate; the step 3 row gains "replace `SEAM[STEP3]`; `/ready` to report db2;
  timeouts for the operational client"; the §5 practices row for the eval marker → **DONE (skeleton)**; and
  a new **Open items carried out of Unit A Project 1** table listing the `bookedAmount` string, the two
  positionally-scripted gather test files, F4 deferred, the untouched `.env.example` modification, and the
  57-distinct-texts corpus.
- `src/dodeal_ai/main.py` — the `backend_keys_missing` log moved after `configure_logging()`, with a comment
  saying why. **This is the change that turned the suite red.**
- `.gitignore` — `.hypothesis/` added.
- `tests/test_assumption_markers.py` — **new**, 7 tests, all passing.
- `CAMPAIGN_REPORT.md` — this block, plus Piece I.7's and Phase I's shas backfilled to `403afa7` (the
  bookkeeping I owed from the start of this phase; leaving `<sha pending>` when the sha is known would make
  the resume mechanism wrong).

### The marker test, and the one place it diverges from the brief

`tests/test_assumption_markers.py` passes 7/7. It asserts a **three-way presence** invariant per marker —
at least one file under `src/`, plus `README.md`, plus `ASSUMPTIONS.md` (`docs/STATUS.md` for
`SEAM[STEP3]`) — rather than the brief's "**exactly three files**".

**Exactly three is not reachable and never was.** The actual counts in `src/` alone:

| Marker | Files under `src/` | Also cited in |
| --- | --- | --- |
| `ASSUMPTION[Q1]` | `pipeline.py` | — |
| `ASSUMPTION[Q6]` | `classify.py`, `schemas.py` | 3 test modules |
| `ASSUMPTION[Q7]` | `pipeline.py`, `state.py` | — |
| `ASSUMPTION[Q8]` | `pipeline.py` | — |
| `ASSUMPTION[Q13]` | `config.py`, `scoring.py` | 3 test modules |
| `SEAM[STEP3]` | `pipeline.py`, `limiter.py` | 1 test module |

Forcing the count to three would mean **deleting markers from code that genuinely depends on them**, to
satisfy a number — the opposite of what the marker is for. The three-way presence check is the invariant
actually wanted: a marker cannot be deleted from the code while the documentation still promises it, and it
cannot be documented without something in `src/` depending on it. A seventh test
(`test_the_marker_set_is_the_five_the_campaign_answered`) greps `src/` for any `ASSUMPTION[Qn]` and fails if
a sixth appears without a ledger entry. `CAMPAIGN_REPORT.md`, `docs/campaign/` and `docs/audit/` are
excluded from the grep, because all three *quote* every marker while discussing the work and would
otherwise satisfy the invariant with their own prose.

### Tree disagreements found in this phase

- **`README.md` claimed `unit_a_v1.txt` still exists** — "a sample, versioned system prompt … the real Unit
  A prompt replaces it when that unit is built" — and that `prompts/structured_intelligence/` was an empty
  reserved directory. Both stale since phase E. **This is the source of the `unit_a_v1.txt` in Piece I.6's
  brief**, which asked for "the ten shipped, including `unit_a_v1.txt`": the brief was written from this
  README row, not from the tree. Corrected — the row now names the nine files that exist and says the
  placeholder is gone.
- **`README.md` claimed `units/structured_intelligence/` was "Empty until the exit demo passes."** Corrected
  to name the nine modules built in phases A–H.
- **`docs/STATUS.md` "Last updated: 29 Aug 2026"** with a 221-test position, ten commits stale. Corrected.

### Still outstanding in Phase J when it resumes

1. Fix the startup test (above), then the four-block chain.
2. The **Final summary** in this report — not written, because §3's format ties it to the phase being
   `DONE`, and writing a final summary above a red suite would be a false record.
3. Stage by path and commit
   `unit-a(J): ledger — README provisional answers, ASSUMPTIONS, STATUS, marker reconciliation`.

**For the lead:**

- **One judgement call is waiting on you and it is a small one:** whether the startup test moves to the
  `json_capture`/`log_capture` channel (my recommendation, and the pattern already exists twice in this
  repo) or whether you want the §6.9 move reverted instead. I have not chosen, because the unattended rules
  said to stop rather than choose.
- **`test_backend_keys_missing_not_logged_when_map_is_non_empty` now passes vacuously** whichever way you
  go. It asserts an absence through a channel that can no longer see anything, so it would pass even if the
  line were emitted. Worth fixing in the same edit.

### Resume, 10 September

**What failed:** `tests/unit/test_startup.py::test_backend_keys_missing_logs_error_when_map_is_empty` —
`1 failed, 802 passed`. Diagnosed correctly on 9 September: the §6.9 move puts the log call after
`configure_logging()`, which does `root.handlers = [handler]` and so evicts the handler `caplog` installs on
the root. The record was emitted and formatted correctly; the capture channel could no longer see it.

**Two things the BLOCKED report got wrong, both mine, both found by the lead:**

1. **`src/dodeal_ai/prompts/unit_a_v1.txt` EXISTS.** Piece I.6 asserted in a committed test comment that it
   "does not exist and there never has been" one, and the Phase J block repeated it as a tree disagreement.
   Both are false. The file sits **one level above** `structured_intelligence/`, and
   `scripts/verify_wheel.py:66` and `tests/unit/test_prompting.py` (four call sites) both require it.
   **The error:** `_PROMPT_DIR.glob("*.txt")` with `_PROMPT_DIR` pointing at `structured_intelligence/` —
   a non-recursive glob that cannot see its parent — and I generalised its result into a claim about the
   whole repository without ever running `ls src/dodeal_ai/prompts/`. The campaign brief's "ten shipped,
   including `unit_a_v1.txt`" was right all along, and the README row I "corrected" in this phase was right
   before I touched it. Fixed: the README row now describes it accurately as the Phase 0 placeholder the
   wheel check and the prompting tests are built against, the package ships **ten** prompt files, and the
   false comment in `tests/security/test_unit_a_injection.py` is replaced with one that says why this suite
   scopes itself to the nine in `structured_intelligence/` (they are the templates a note reaches). The
   nine-file assertion itself is correct for that directory and stays.
2. **The route paths in the README were invented.** I wrote `/api/v1/judgements/note` and
   `/api/v1/judgements/note/resubmission` from the campaign brief's §2.5 prose. The router
   (`api/routes/judgements.py:43,57,74`) mounts `prefix="/api/v1"` plus `/notes/judgements` and
   `/notes/judgements/resubmission`. Corrected to the router's paths, with `GET /api/v1/meta/versions`
   named alongside. The code is the fact, and I did not check it.

**The six fixes applied:**

| # | Fix |
| --- | --- |
| 1 | `tests/unit/test_startup.py` rewritten to capture through a `StreamHandler(JsonFormatter())` on the **`dodeal_ai`** logger — the `json_capture` / `log_capture` pattern already used twice in this repo. It survives `configure_logging()` because that function sets the `dodeal_ai` logger's *level* and never touches its *handlers*. The presence test asserts a real JSON line with `level == "ERROR"`, `logger == "dodeal_ai.startup"`, and the `backend_keys_missing` event. The absence test now **logs a probe line first and asserts the channel saw it**, then asserts `backend_keys_missing` is absent — so it can no longer pass vacuously, which is exactly how it survived the §6.9 move while its partner failed. The `main.py` move is kept. |
| 2 | `tests/test_assumption_markers.py`: `check=False` on both `subprocess.run` calls (ruff `PLW1510`, the two errors the chain never reached). Nothing else. |
| 3 | `README.md`: route paths corrected to the router's. |
| 4 | `README.md`: the `unit_a_v1.txt` row rewritten as above, the file kept; the false comment in `tests/security/test_unit_a_injection.py` corrected. |
| 5 | `docs/STATUS.md` §6: **Q16–Q21** added. Open-items table gains `DECISION[DIRECT_ROUTE]` (accepted 10 September, Piece K, not built, the one exception to "no note text in a request body") and `tenant-c.json` not vendored. |
| 6 | This block, and the Final summary below. |

**Also corrected while here (bookkeeping the resume owed):** Pieces I.4, I.5 and I.6 still read
`STATUS: DONE <sha pending>` — backfilled to `d0c9acf`, `f17da28`, `257da13`. And `docs/STATUS.md` carried
`317619b` for Phase C in two places; the real sha is `317619f`. Both were stale-placeholder errors in the
resume mechanism itself, which is the one document that must not be wrong.

**Tests:** 0 net added (2 rewritten). Suite **803 passing, 1 skipped, 7 deselected, 99.32 %**; all 13
per-file floors met; wheel builds and imports.

**For the lead:**

- **ASSUMPTIONS §3.6 is missing all three Unit B entries.** B7 (access audit), B2 (Arabic summary) and B13
  (storage country) appear nowhere in the file — `grep` for `B7`, `B13`, "access audit", "storage country"
  and "Arabic summary" returns nothing. §3.6 has 13 rows and none of them is these. **Not written**, per
  your instruction to record and not write.
- **`test_the_shipped_set_is_the_nine_files_that_exist` is now a misleading test NAME** — nine is the count
  of `structured_intelligence/`, not of the package, which ships ten. The comment inside now says so
  plainly. Renaming it was outside "fix only the comment", so I left it.
- **`docs/security/owasp-llm-unit-a.md` says "all nine shipped templates"** in the LLM05 paragraph. Its
  scope line correctly says "the nine prompt templates in `src/dodeal_ai/prompts/structured_intelligence/`",
  so it is not wrong — but "shipped" is doing loose work now that the package demonstrably ships ten. The
  file was not in this commit's staging list, so it was not touched.
- **`AIService.zip` is untracked in the working tree again.** It had disappeared before Piece I.4 and is
  back. Never staged, never touched.

---

## Final summary

**Campaign: Unit A Project 1, phases A–J. Complete.**

### Phases and shas

| Phase | Title | Sha |
| --- | --- | --- |
| A | unit schemas and the `TenantConfig` seam | `df4689b` |
| B | db2 operational client and the three state concerns | `42cd11c` |
| C | judgement routes, `TenantScope`, `DodealError`, `SEAM[STEP3]` | `317619f` |
| D | classification against FakeLLM, `system_event` short-circuit | `8874958` |
| E | vague detection: per-type prompts, fixed missing-components vocabulary | `b432af1` |
| F | scoring: marks from the model, arithmetic in code, Q13 suppression | `2f2dbfb` |
| G | reprompt once via `AssembledPrompt.tail`, then 503 | `103ce02` |
| H | decide, the clarification loop, the rate limit | `ae62103` |
| I.1 | vendor fake CRM fixtures | `ba44c5c` |
| I.2 | FakeLLM prompt-directed scripting | `335abf4` |
| I.3 | reprompt tail without the previous-answer fiction | `86a0b3c` |
| I.4 | relative times and closures in the templates | `d0c9acf` |
| I.5 | few-shot examples | `f17da28` |
| I.6 | adversarial suite | `257da13` |
| I.7 | OWASP note and eval skeleton | `403afa7` |
| J | ledger — README provisional answers, ASSUMPTIONS, STATUS, marker reconciliation | `d93d936` |

### Suite at head

**803 passing · 1 skipped · 7 deselected · 99.32 % coverage.** Repo gate 92 %; **all 13 per-file floors
met** (`core/auth/**` 95, `core/tenancy.py` 100, `core/cost/**` 95, `core/errors.py` 95,
`core/validation.py` 100, `core/log_safety.py` 100, `units/structured_intelligence/**` 95 with `config.py`,
`scoring.py`, `llm_call.py` and `decide.py` at 100 and `state.py`/`pipeline.py` at 95). ruff check, ruff
format --check and mypy clean over 59 source files. `uv build` + `scripts/verify_wheel.py`: import check OK.
The 1 skipped is `tests/eval/test_quality_eval.py` (needs a real model, step 18); the 7 deselected are the
`integration` marker.

### Every decision taken

**Contract and arithmetic.** Marks come from the model; total, denominator and band are computed in code and
never accepted from any input (F). Suppression is a *state* carrying `mark: null, suppressed: true`, never a
zero — the weight leaves the denominator (F). Judgements are version-stamped and never recomputed (A).
`system_event` and `unclassifiable` stop after classification, costing one pass rather than three (D).
Thin evidence stops before any reservation and any model call, costing nothing (C).

**State and failure policy.** Three concerns, three different policies, deliberately not unified (B):
idempotency unavailable → **deny** (503), rate-limit and attempt stores unavailable → **open** with a log
line. The idempotency key is reserved after the fetch and before any model call, and released on every
non-200 outcome after reservation, so a `model_unavailable` is retryable without a 409 (C). The attempt key
is on lead+note, not on the person. The fingerprint is SHA-256 of the fetched text with **no
normalisation**. Hitting the rate limit returns **200 with `prompt_withheld: "rate_limited"`, never a 429**
(H).

**Prompts.** Every prompt is a versioned file; `build_prompt` is the only assembly path; no inline prompt
strings (E, F). One reprompt on malformed output via `AssembledPrompt.tail`, then 503 — never a retry, and
the rejected answer never re-enters a prompt (G). The tail says nothing about a past the model cannot see
(I.3). Relative times and explicit closures satisfy `next_step_date` (I.4). Few-shot examples live in the
stable half so they never vary per note (I.5).

**Test infrastructure.** Vendor data the schemas reject is skipped, counted and pinned — never repaired
(I.1). An existing-but-empty template queue raises rather than falling through to the positional script
(I.2). Delimiter claims are about **counts**, never absence (I.6). "One clear case per type" is satisfied
across the seven templates rather than within `classify_v1.txt`, which is what let the 3–5 example cap
stand (I.5).

### Every tree disagreement found

| Where | What | Resolution |
| --- | --- | --- |
| F | §2.3 predicts denominator **75** for no_contact with Q13 resolved. Unreachable from §2.2's weights: no_contact suppresses `client_said` *and* `deal_specifics` by type, so lifting Q13 leaves 25+25+10 = **60**. | Tree followed; recorded in ASSUMPTIONS §3.4. |
| H | `clarification_cap` is 1, so `attempts_remaining` is only ever 1 or 0. | Config, not a decision; recorded. |
| I.1 | Fixture lead `1661` carries `bookedAmount: "1,250,000"`; `Lead` says `float \| None`. | Skipped, counted, pinned at 1. **Still open — needs the real export.** |
| I.6 | The corpus reuses note bodies: 127 notes, **57 distinct texts**. | Test asserts the variable half tracks note *text*. |
| I.6 / J | "No `unit_a_v1.txt`." | **My error, corrected in J.** The file exists one directory up. |
| J | `README.md` claimed `units/structured_intelligence/` was "Empty until the exit demo passes" and STATUS.md read "221 tests, 29 Aug". | Both corrected. |
| J | README route paths were invented from the brief's prose. | **My error**, corrected to the router's paths. |
| J | Markers cannot appear in "exactly three files" — several are load-bearing in two `src/` files each. | Test asserts three-way **presence** instead; counts recorded. |

### What the lead must review in H

- **`§2.6`'s "when the attempt counter exists" now means "when the reference beside it exists".** Two db2
  entries; a partial outage can leave one without the other, and the reference is then returned. More
  truthful than inventing one, but not the letter of the amendment. Gating the read on `read_attempts() > 0`
  is one `if` and one extra db2 read.
- **`model_version_mismatch` is a new log-only code**, directed by ruling §2.2 and absent from §2.1's list.
  Either adopt it or say to drop the line.
- **Audit S2-6 is untouched and now adjacent.** `RESUBMIT` still has no gate-2 or gate-4 assertion, and H
  gave that route real behaviour of its own for the first time.
- **Register item 63** (the dangling sibling on the gather failure path) is unchanged and no worse.

### What step 3 must now replace

1. **`SEAM[STEP3]`** — the no-op token pre-flight stub in `units/structured_intelligence/pipeline.py` and
   the matching marker in `core/cost/limiter.py`. Real token counting and `enforce_token_cost`.
2. **`/ready` must report db2.** Today it pings the cost client only, so a service whose operational store
   is unreachable reports ready and then denies every judgement with `503 idempotency_unavailable`.
3. **Socket timeouts for the operational client** — still hardcoded at 2.0s, the last hardcoded operational
   value in the service, together with the H3 breaker.

### Open items, collected

| # | Item | Owner |
| --- | --- | --- |
| 1 | `bookedAmount` may be a string — confirm against the real export | backend (Waqas) |
| 2 | ASSUMPTIONS §3.6 missing B7 / B2 / B13 | lead |
| 3 | Two test files still script the vague/score gather positionally | us |
| 4 | Review finding F4 deferred — one tail for both form and content failures | post-campaign |
| 5 | The corpus is 57 distinct texts, not 127 — matters before any quality number | before step 18 |
| 6 | `DECISION[DIRECT_ROUTE]` accepted, Piece K, **not built** — the one exception to "no note text in a body" | us |
| 7 | `tenant-c.json` not vendored — no second-tenant corpus exists | us |
| 8 | `.env.example` modified-unstaged throughout; never touched | whoever made it |
| 9 | `classify_v1.txt` shows 5 of 8 possible answers | lead |
| 10 | Q16–Q21 newly raised in STATUS §6 — **Q20 (data residency / DPA) is a hard gate before any real note reaches a provider, and is unowned** | per row |
| 11 | `test_the_shipped_set_is_the_nine_files_that_exist` is a misleading test name | us |

**Nothing in this campaign has been verified against a real model or a real backend. Nothing is marked
`[V]`.** See ASSUMPTIONS §8.11.

---

## Piece K: direct judgement route   STATUS: DONE d9e0486

Not a campaign phase. One commit adding the route the lead decided on, plus the second commit that
backfills this sha. **The campaign's never-list item "accept note text in a request body" is amended
here, for these two routes only**, on the lead's decision.

**What changed:**

| File | What |
| --- | --- |
| `src/dodeal_ai/units/structured_intelligence/schemas.py` | `NOTE_TOO_LONG` added to `SuppressedDetail` (pairing docstring rewritten: `note_too_short` → `insufficient_evidence`; `system_event`, `unclassifiable`, `note_too_long` → `not_scorable`). `LeadContext` (four nullable lead fields, `extra="forbid"`) and `DirectJudgementRequest` (`lead_id`, `note_id`, `author_id`, `note_text` capped at `MAX_NOTE_TEXT_CHARS = 4000`, `lead`, `extra="forbid"`). No band, no total, no score field of any kind. |
| `src/dodeal_ai/units/structured_intelligence/config.py` | `max_note_chars` beside `min_note_chars` (2000 in `_DEFAULT_CONFIG`); `config_version` → `tenant-cfg-default-2`. |
| `src/dodeal_ai/units/structured_intelligence/pipeline.py` | `_is_thin` → `_length_gate`, returning the `(reason, detail)` pair or `None` and now covering both ends. `judge_note` keeps steps 1–2 and hands to the new `_judge`; `judge_note_direct` builds a `Lead` + `LeadNote` from the body and enters `_judge` at the length gate. `_judge` is everything from there down, unchanged. `JudgementDeps.leads` is `LeadsClient | None`. `_log_outcome` gains `author_differs_from_subject`, present on direct-route lines only. |
| `src/dodeal_ai/api/routes/judgements.py` | `POST /api/v1/notes/judgements/direct` and `.../direct/resubmission`, both behind `Depends(gate4_cost)` exactly like the existing routes. `get_leads_client` is not in either dependency tree. |
| `src/dodeal_ai/core/errors.py` | Validation-handler docstring qualified "on the primary route", and says what the direct route's over-length 422 carries. |
| `README.md` | Direct-route paragraph under the route contract; the never-accepted claim qualified; a security bullet added for it. |
| `docs/security/owasp-llm-unit-a.md` | LLM07: one paragraph — on the direct route the note arrives in the body, is validated by the schema, and is delimited by `build_prompt` exactly as fetched text is. |
| `ASSUMPTIONS.md` | New §3.7 `DECISION[DIRECT_ROUTE]` — decision, reason, the three points, the correction paths. |
| `docs/STATUS.md` | Piece K row, the `DECISION[DIRECT_ROUTE]` open item marked built, suite numbers. |
| `tests/unit/test_direct_routes.py` | New, 21 tests. |
| `tests/security/test_log_safety.py` | New direct-route section: five outcomes for the note, four for `lead.project`, root logger at DEBUG. |
| `tests/eval/test_structural_eval.py` | `NOTE_TOO_LONG_COUNT = 3` pinned; `NOTE_TOO_SHORT_COUNT` unchanged at 9; a test that the over-long notes cost nothing. |
| `tests/unit/test_unit_a_schemas.py`, `test_unit_a_config.py`, `test_judgement_routes.py` | The introspection test extended to the request models; the four-member detail vocabulary; the `config_version` pin moved to `tenant-cfg-default-2` in three places. |

**The lead's decisions, recorded not argued.** The route accepts note text because the CRM read surface has
been unavailable for six weeks and the CRM sends the saved note server-side after the save. The credential is
the note author's forwarded user JWT, so the gates and both counters are unchanged and key on `sub`.
`author_id` is trusted as the CRM's stored author, never checked against `sub`, never a rejection — the
difference is logged as `author_differs_from_subject`, ids only. Two length limits: 4,000 hard (422) and 2,000
soft (`not_scorable` / `note_too_long`, both routes). Both provisional; the sample's longest note is 340.

**Decisions taken here, and their cost:**

1. **`_length_gate` returns a pair, not a bool.** The alternative was a second gate function for the upper
   bound. **Cost of the alternative:** two functions counting "the text" separately, one call site each, and
   the first quiet disagreement about whether the count is before or after `strip` becomes a scoring
   difference nobody sees. One function, one call site, both bounds.
2. **`JudgementDeps.leads` is `LeadsClient | None`, and `_fetch_note` asserts it.** The alternative was a
   stand-in client whose methods raise, passed on the direct routes. **Cost of the alternative:** a
   `# type: ignore` on a concrete class, and a reader who sees a leads client on a route that must never
   fetch. `None` is the true statement; the assert is one line on a path that always has a client.
3. **`note_too_long` is `not_scorable`, not a third `SuppressedReason`.** As the lead specified. The reason
   vocabulary answers "why is there no score", and "we will not score this" already exists — the trouble with
   a 5,000-character note is not that there is too little evidence but that it is not one interaction.
4. **The soft limit applies to both routes.** The lead's instruction, and the pipeline makes it free: the gate
   is in `_judge`, which both entry points run. Three corpus notes move from scored to suppressed as a result.

**Tree disagreements — three, all mechanical, all resolved toward the prompt's intent:**

1. **`max_note_chars: int = 2000` cannot carry an inline default where the prompt places it.** `TenantConfig`
   is a frozen `slots` dataclass and **no field on it has a default**; a defaulted field beside
   `min_note_chars` is followed by nine fields without defaults, which is a `TypeError` at class creation.
   **Resolved:** declared beside `min_note_chars` with no inline default, in the style of every other field,
   and the value `2000` set in `_DEFAULT_CONFIG` beside `min_note_chars=15`. Placement and value are as
   instructed; only the inline-default spelling could not be.
2. **README has no security bullet reading "posting note text is a 422".** That phrase is in
   `core/errors.py:181` — which the prompt separately (and correctly) names. The README claim that needed the
   qualifier was the route-contract sentence "note text is never accepted in a request body".
   **Resolved:** that sentence now ends "on this route", and a **new** security bullet was added carrying the
   "on the primary route" qualifier the prompt asked for, so the bullet the prompt describes now exists.
3. **`lead.project` on five paths, not four.** The prompt asks for the note sentinel on five outcomes and the
   `LeadContext.project` sentinel on "the same four paths". The over-4,000 case is specific to `note_text`,
   so `project` is covered on the other four: 200, 422 (extra field), 503, 500. Read as four, built as four.

**Tests:** 38 added (803 → **841 passing**, 1 skipped, 7 deselected). Coverage **99.34 %** (was 99.32).
All 13 per-file floors met, no new floors: `pipeline.py` **100 %** against its floor of 95; `decide.py` and
`llm_call.py` **100 %**. `uv build` + `scripts/verify_wheel.py` OK in a clean venv.

**For the lead:**

- **The rate limit still keys on `sub`, and on this route that is a decision with a shape.** If the CRM ever
  forwards a supervisor's JWT for a note a salesperson wrote, the supervisor's bucket is the one that fills.
  The correction path is recorded (`state.py`'s key builder switches to `author_id` for this route) and the
  evidence for taking it is now collectable: `author_differs_from_subject` on the outcome line. **Nothing
  reads that field yet** — someone has to actually look at it, or the correction path has no trigger.
- **Three corpus notes changed outcome.** The fixture's three notes over 5,000 characters were scored before
  this piece and are suppressed `note_too_long` now. The eval pins 3; the pin is safe because every other
  corpus note is under 400 characters, so nothing between 400 and 5,000 exists to make the count sensitive to
  where the limit sits. **A real corpus will not have that gap.**
- **`config_version` moved to `tenant-cfg-default-2`.** Nothing is rescored, by design. Any judgement stored
  under `-1` is still readable and still comparable only with other `-1` judgements. If the CRM has stored
  judgements from testing, they now carry the older stamp — which is correct, and worth knowing.
- **The direct route has no read-after-write problem, and the fetch route's candidate debt is unchanged.**
  Step 4's bounded re-read (STATUS §2) exists because a replica may not have the note yet. The direct route
  cannot hit that — the CRM sends the text — which is a real argument for the route beyond the outage, and an
  argument against removing it the moment the read surface returns. **Not a decision I am taking**; recorded
  because the piece surfaced it.
- **Nothing here is verified against a real model or a real backend.** Unchanged, and still ASSUMPTIONS §8.11.

---

## Final summary

Pending — written when Phase J is `DONE`.

---

## For the lead

Running list, appended by every phase. Nothing here blocks the campaign.

- **Phase 0 — campaign prompt truncated at Phase C.** The prompt states "ten phases" (A–J) and
  specifies Phase 0, A, B and C in full; the text ends at Phase C's commit subject. Phases D–J
  are named as placeholders above. D is referenced from Phase C ("a grep test in Phase D
  enforces it" — no inline `AssembledPrompt` construction in `src/`) and H is referenced from
  Phase C's floors ("pipeline.py 90, raised in H"), so those two have known content. **Send the
  D–J sections before this session reaches the end of Phase C.**
- **Phase 0 — Q8 is not spelled out in the prompt.** §3.6 says only "ASSUMPTION[Q8] at the fetch
  (nothing branches on it)". I have recorded the page-one/note-ordering reading above because it
  is the fetch-shaped assumption §1's Design A states. If Q8 is actually the
  `updatedAt`-bump question (ASSUMPTIONS §4.2), correct the row — no code depends on which it is.

---

## Piece L: hardening trio   STATUS: DONE a6eca66 · 85aa3e0 · fc346bc

Not a campaign phase. Four register items in three commits plus this backfill, all of them the kind of
thing that is invisible until the day it is not: a task nobody cancelled, a queue nobody bounded, a
blocking call nobody noticed, and a latency number nobody could see.

**Suite:** 841 → **904 passing**, 1 skipped, 7 deselected. Coverage **99.38 %** (was 99.34). All 13
per-file floors met, no new floors. ruff/format/mypy clean at every commit.

| Commit | Floor after | Suite after |
| --- | --- | --- |
| `a6eca66` — L.1 | 13/13, `pipeline.py` 100 % against 95 | 855 passing, 99.35 % |
| `85aa3e0` — L.2 | 13/13 | 892 passing, 99.37 % |
| `fc346bc` — L.3 | 13/13, `pipeline.py` 100 % | 904 passing, 99.38 % |

---

### L.1 — cancel the gather sibling (register item 63)   `a6eca66`

**The bug, stated exactly.** `asyncio.gather(..., return_exceptions=False)` propagates the first
exception and **leaves its siblings running**. When the vague pass failed, the scoring pass carried on:
the request had already 503'd and released its idempotency key, and the abandoned pass then took its
answer, found it malformed, logged `reprompt_issued` and spent a **fourth** model call on a judgement
nobody would ever receive. It was recorded as "unchanged and no worse" at Phases F, H and J; this closes it.

| File | What |
| --- | --- |
| `src/dodeal_ai/core/resilience.py` | `gather_or_cancel(*coros)`, plus `_first_exception` and `_cancel_and_drain`. Two overloads: a generic two-argument form (the only shape either caller needs) and a variadic `Any` fallback. |
| `src/dodeal_ai/units/structured_intelligence/pipeline.py` | One code line: the gather at the vague/score call site. `import asyncio` goes with it — it was used for nothing else. The module docstring's `asyncio.gather` paragraph is rewritten to describe what the code now does. |
| `tests/unit/test_resilience.py` | 8 added: order preserved, the empty call, the sibling cancelled, the sibling **awaited** and not merely cancelled, the exception object re-raised by identity, either side may fail, argument order decides which failure is raised, caller cancellation propagates and takes both children with it. |
| `tests/unit/test_judgement_pipeline.py` | 6 added: both failure directions, both 503s the gather can produce, the key still released, and a sentinel note through the failing pass. |

**The three promises, and where each is enforced.** *Nothing is left running* — the cancel-and-drain is
in a `finally`, so it holds on the failure path, the caller-cancellation path, and any path a future
edit invents; and it **awaits**, because `task.cancel()` only schedules a `CancelledError` and returning
without the await is the leak itself. *The first exception is re-raised unchanged* — the same object, so
`ModelUnavailableError` still reaches the pipeline's release-on-error block as itself and a 503 does not
become a 500. *A caller's `CancelledError` is never swallowed* — a helper that caught it to do its
cleanup and returned normally would make a cancelled request look like a completed one and leave both
model calls running.

**The tests have teeth, and this was verified rather than assumed.** The held pass in each is scripted
**malformed-then-good** and released by the test only *after* the request has already failed. A cancelled
task is gone by then and nothing happens; a leaked one wakes up, logs `reprompt_issued` and spends a
fourth call. Reverting the one pipeline line to `asyncio.gather` **fails three of them**, with the
leaked pass's `reprompt_issued` visible in the captured log. A test that only asserted "the exception
came out" would have passed with the bug in place.

**Decisions taken here, and their cost.**

1. **"First" means argument order, not completion order.** `FIRST_EXCEPTION` returns as soon as one task
   raises, so in practice exactly one has failed — but two can complete in the same loop iteration, and
   then "first" has to mean something. **Cost of the alternative** (completion order): a call site
   cannot predict it, so a test pinning which of two failures surfaces would be pinning a scheduling
   accident. Argument order is the only rule visible from the call site.
2. **Overloads rather than `Any`.** `a, b = await gather_or_cancel(x, y)` keeps its types, so the
   unpacking on the next line is still checked. **Cost of the alternative:** `vague_result` becomes
   `Any` and mypy stops checking every field access downstream of the gather.
3. **The helper lives in `core/resilience.py`, not in the unit.** It is the same kind of statement
   `call_with_watchdog` is — the one place that says what happens to a call that goes wrong — and item 9
   needs it from a different module. **Cost of the alternative:** a second copy in the tool layer, and
   the first divergence between them is a leak nobody is looking for.

**Item 9 (gather the lead and notes fetches) is NOT in this piece**, as instructed. The two-argument
overload is exactly its shape, so it is a call-site change when it lands and nothing here moves.

---

### L.2 — load shedding and the sync-client guard (items 73, 75)   `85aa3e0`

| File | What |
| --- | --- |
| `src/dodeal_ai/core/config.py` | `max_inflight: int = Field(default=32, gt=0)`. **Provisional**; the load lane sets the real number. `0` would refuse every request including the first — a config typo indistinguishable from an outage — so it fails closed at settings load. |
| `src/dodeal_ai/middleware/inflight.py` | New. `InflightCounter` (acquire/release/count, no lock — one loop, no await between the compare and the increment), the module-level counter, `current_inflight()`, and `InflightMiddleware`. |
| `src/dodeal_ai/core/errors.py` | `LoadShed` (`load_shed`, 503) and `dodeal_error_response(exc, request_id)`. The handler now returns through it too, so there is one body builder and not two. |
| `src/dodeal_ai/main.py` | `InflightMiddleware` registered **before** `RequestIDMiddleware`, which puts it **inside** — with the comment that says why, because the reversal is the thing a reader gets wrong. |
| `tests/unit/test_inflight.py` | New, 22 tests. |
| `tests/test_no_sync_clients.py` | New, 8 tests (7 patterns + a fail-closed check on the glob). |
| `tests/security/test_log_safety.py` | The refused request as a sixth outcome: the note, `lead.project`, and the shed line carrying no tenant and no body. |
| `tests/unit/test_dodeal_errors.py` | `LoadShed` in `_CODES`; `dodeal_error_response` builds the same body the handler would. |
| `README.md`, `ASSUMPTIONS.md` | The error list, the config reference, the middleware table, a new repo-wide-guards test table, the CI note; ASSUMPTIONS §10.1. |

**Where it sits is the whole design.** The refusal has to be cheaper than the work it refuses or it is
not a defence. **After** the request id, so a refused caller has something to quote in a support ticket
and the `WARNING` line correlates with their retry. **Before everything else**, so a refusal costs a
counter comparison and no token verification, no tenant resolution, no body read, no Redis round-trip.

**The ordering trap, recorded because it is silent.** Starlette's `add_middleware` **inserts at the
front** of `user_middleware`, and `build_middleware_stack` wraps `reversed(middleware)` — so the front of
that list is the **outermost** layer and the **last** `add_middleware` call is the **first** middleware a
request meets. "Install it after the request-id middleware" therefore means *register it before*.
Getting it backwards does not break anything visibly: the refusal keeps working and quietly carries
`request_id: "unknown"`. `test_the_refusal_carries_the_request_id` is what pins it.

**What the line carries, and what it must not.** The id and the count. **Never a tenant** — the gates
have not run, so the only tenant available is the caller's own unverified claim about itself, and a log
field a header can set is worse than no field. **Never a body** — it was never read, and reading one in
order to log it would be precisely the expense this exists to avoid. The count and not the cap: the cap
is in config and a reader can look it up, whereas how many were actually in flight when the refusals
started is the number that says whether the cap is wrong.

**The concurrency test is the one that matters.** Twenty requests at once against a stub slow route
through an `httpx.ASGITransport` — one loop, so they really are concurrent, which `TestClient` (a thread
per request) cannot produce. Exactly five admitted against a cap of five, exactly fifteen refused, and
the admitted five are the *first* five to arrive. **Verified by breaking it**: putting a single
`await asyncio.sleep(0)` between the compare and the increment admits all twenty and fails six tests in
that file. "Some were refused" would have passed with the race in place.

**Item 75.** One test file, seven patterns, styled on the shipped-template greps in `test_scoring.py`
and `test_unit_a_injection.py`: read every module under `src/dodeal_ai/`, search, and fail naming the
file and the line with the async spelling to use instead. It fails closed if the glob ever matches
nothing, so a package rename cannot silently retire the guard. `time.sleep` and `urllib.request` are on
the list although neither is a client: they block the loop identically, and the audit's Category I sweep
already wants `time.sleep` gone on a determinism ground.

**Decisions taken here, and their cost.**

1. **503 `load_shed`, not 429.** 429 is Gate 4's, and it means "you have had your share". This means
   "the service is full right now". **Cost of the alternative:** a CRM that back-offs per-user for a
   condition that is per-pod, and a 429 rate in the dashboards that mixes two unrelated causes.
2. **`dodeal_error_response` added rather than exporting `_unit_error_body`.** Middleware cannot raise a
   `DodealError` — `ExceptionMiddleware` is built *inside* the user middleware stack, so it would come
   back as a generic 500. **Cost of the alternative** (building the dict in the middleware): two
   spellings of the error body, and the first divergence is a CRM branching on a `reason` that only some
   refusals carry.
3. **The counter is a named object, not a module-level `int`.** **Cost of the alternative:**
   `_count -= 1` in a `finally` somewhere in a dispatch method, and "every increment is matched by a
   decrement" becomes something a reader has to trace rather than read.
4. **No lock.** One event loop, one process, and no `await` between the compare and the increment.
   Recorded rather than assumed, because it is the assumption that makes the exactness test meaningful.
5. **`/health` and `/ready` exempt by exact path, not prefix.** `/healthz` and `/health/../x` are
   counted like anything else.

**Tree disagreements — two, both resolved toward the prompt's intent.**

1. **The README has no error *table*.** The enumerated codes are one prose line ("Error bodies are a
   fixed `{detail, reason, request_id}`: …"). **Resolved:** `load_shed` 503 appended to that line, which
   is the list the prompt means, and a full row added to the middleware table where `inflight.py` now
   sits beside `request_id.py`.
2. **The CI job list is a list of *commands*, mirroring `ci.yml` step for step.** Adding a seventh row
   for `test_no_sync_clients.py` would claim a CI job the workflow does not have, and STATUS's own rule
   is that when the file and the tree disagree the tree is right. **Resolved conservatively:** the guard
   runs inside check 4 (`uv run pytest`), and a paragraph under the table says so and names both
   repo-wide guards; a new `tests/` table documents the two files themselves. **For the lead** — see below.

---

### L.3 — elapsed milliseconds and in-flight count (register item 72)   `fc346bc`

| File | What |
| --- | --- |
| `src/dodeal_ai/units/structured_intelligence/pipeline.py` | `_Timings` (frozen, four durations + `fields()`), `_ms_since`, `_timed`. Both entry points take `time.monotonic()` as their first statement and pass it to `_judge`. `_log_outcome` takes `timings` and spreads five numbers onto both events. |
| `tests/unit/test_judgement_pipeline.py` | 8 added: the five on a scored judgement, three nulls on a length-gate suppression, one number and two nulls on a classification suppression, nothing else on the line changed, the fetch is inside `elapsed_ms`, `inflight` read from the middleware's counter, no field is a wall-clock reading, and the module reads `time.monotonic`. |
| `tests/unit/test_direct_routes.py` | 4 added: the five on the direct route, scored and suppressed; the same five on the fetch route; and `inflight == 1` for a request that is genuinely in flight. |
| `tests/security/test_log_safety.py` | The happy-path sentinel test now asserts the five are on the line it clears, so they are **inside** "no note text on any log line" rather than beside it. |

**`time.monotonic()`, never a wall clock.** `datetime.now()` can go **backwards** across an NTP
correction, and a negative duration in a latency panel is not a small error — it is a number nobody can
interpret. These fields say how **long**, never **when**: nothing here is a timestamp, and
`test_no_timing_field_is_a_wall_clock_timestamp` fails if one ever becomes one.

**The clock starts at the entry point.** On the fetch route that puts the two backend calls inside
`elapsed_ms`, which is the point — they are most of what a slow judgement on that route is, and a
measurement beginning inside `_judge` would have been silent about them. A deliberately slowed
`get_lead` proves it: `elapsed_ms >= 20` while `classify_ms` stays under 20, so the delay is in the
total and demonstrably not in the passes.

**Null, not zero, for a pass that did not run.** A suppressed note did not take no time to score; it did
not score. Zero would average into a latency panel as a fast pass and quietly drag the number down. So a
length-gate suppression carries three nulls, a classification suppression carries one number and two
nulls, and only a scored judgement carries three numbers.

**A clock each for the gathered passes.** One pair of readings around the gather would measure the
slower of the two twice and say nothing about the other. Their sum can therefore **exceed** `elapsed_ms`,
because they overlap — that being the whole reason they are gathered — so nothing here is a breakdown of
the total, and `_Timings` says so where someone will read it.

**Decisions taken here, and their cost.**

1. **`inflight` is read at outcome time, inside `_log_outcome`.** The question it answers is "what else
   was this pod doing when this judgement finished". **Cost of the alternative** (capturing it at entry):
   a number about a moment that has passed, on a line about a moment that has not.
2. **The unit imports `current_inflight` from `middleware/inflight.py`.** A unit holds a `TenantScope`
   and no `Request`, so `app.state` is unreachable from it; the module-level counter is the only thing it
   can read. The dependency is one function and one direction, and `middleware/` imports nothing from
   `units/`. **For the lead** — see below.
3. **A `_Timings` dataclass rather than four keyword arguments.** Four `int | None` in a row is exactly
   the signature a caller gets wrong. **Cost of the alternative:** `_log_outcome` grows to eight
   parameters and the four that are interchangeable by type sit next to each other.
4. **`_timed` wraps the coroutine rather than the pipeline timing around the gather.** Keeps
   `gather_or_cancel` ignorant of timing, which is right — it is about cancellation.

---

### For the lead

1. **`elapsed_ms` is NOT on `external_call_failed` or `reprompt_issued`, deliberately.** The prompt
   allowed either, conditional on the emitting site being able to see a start time. Neither can see the
   **judgement's**: `external_call_failed` is emitted in `core/resilience.py`'s `call_with_watchdog` and
   `reprompt_issued` in `units/.../llm_call.py`, and both are several frames below `_judge` with no
   timestamp in scope. Each *could* trivially time its own attempt — but then `elapsed_ms` would mean
   "the whole judgement" on two lines and "this one attempt" on two others, in the same log stream, and
   the repo's own rule is that two meanings for one number is how a line starts disagreeing with itself.
   **Recorded rather than plumbed**, as instructed. If you want per-attempt durations they should be a
   differently named field (`attempt_ms`), which is a decision, not an omission.
2. **The sync-client guard is not a CI *job*.** The README CI table mirrors `ci.yml` step for step, and
   `ci.yml` has no such step — the test runs inside `uv run pytest` (check 4). Adding a row would have
   made the README claim a job that does not exist. Say the word if you want a genuinely separate CI step
   and it is four lines of `ci.yml` plus the row.
3. **The `requests.` pattern matches English prose.** It caught a sentence in a docstring I had just
   written ("…already holds `max_inflight` requests.") and the docstring was reworded rather than the
   pattern loosened, because the pattern list is specified. It will do this again to anyone who ends a
   sentence with the word "requests" inside `src/`. `\brequests\.\w` would fix it and would still catch
   every real call; that is a one-character decision that is yours, not mine.
4. **`middleware/inflight.py` has no per-file coverage floor.** It is at 100 % today. The floors are a
   deny-path and judgement-module policy and adding a fourteenth is a change to that policy, so it was
   not made. It is arguably a deny path now — it refuses requests — and worth a floor of 95 if you agree.
5. **`units/` now imports from `middleware/`**, for `current_inflight` only. One function, one direction,
   no cycle. The alternative is moving the counter into `core/` and letting the middleware import it from
   there, which is probably where it belongs if a second reader ever appears.
6. **32 is a placeholder and will be wrong.** It is not a measurement of anything. Until the load lane
   sets it, a `load_shed` line in production means "this number is wrong", not "capacity was reached" —
   and that reading is written into the config comment, the README and STATUS so it cannot be lost.
7. **The Phase F, H and J reports still say register item 63 is "unchanged and no worse".** That was true
   when written and has been left alone rather than edited; this block is where it is closed.
8. **`DODEAL_MAX_INFLIGHT` is per pod.** Two replicas shed against their own ceilings, which is what a
   per-pod ceiling means and is the only thing an in-process counter can do. A cluster-wide limit is the
   edge's job, alongside Q21's body-size limit.

**Nothing in this piece has been verified against a real model, a real backend or real load.** Nothing is
marked `[V]` (ASSUMPTIONS §8.11). In particular the load-shedding behaviour has been proved *correct*
under twenty concurrent in-process requests and not *sized* against anything.

---

## Piece M: model profiles on the LLM seam   STATUS: DONE b263c8b

Not a campaign phase. Register item 77, one commit plus this backfill. The seam has always been able
to say *what* to send; it could not say *which model* should read it. This is that, and it is
deliberately the smallest version of it that a caller can use and an adapter can consume.

**Suite:** 904 → **934 passing**, 1 skipped, 7 deselected. Coverage **99.40 %** (was 99.38). All 13 per-file
floors met, no new floors. `llm_call.py` and `decide.py` stay at 100.

---

### R17 — profiles enter on the method, not on the factory

*Refines master document §9.3.*

**The constraint that decided it.** Every test in this repo injects `FakeLLM` through
`app.dependency_overrides[get_llm_client]`, and `get_llm_client` takes no arguments. A factory that
took a profile — `get_llm_client(profile)` — would be a different dependency per task, so the one
override every test uses would stop covering all three passes on the day it landed. That is not a
cost worth paying for a keyword that fits on the method.

So: **one method, one parameterless factory, and `profile` as a required keyword on `complete`.**

```
complete(prompt, *, profile: str, max_output_tokens: int | None = None)
```

**The division of knowledge.** The caller names its TASK and knows nothing else — not a provider, not
a model id, not a temperature. The adapter owns the table and resolves the name. That is what makes
"run the vague pass on a bigger model" a config change rather than a code change, and it is why the
temperature bullet in `client.py`'s "deliberately NOT on the Protocol" list changed rather than being
deleted: temperature was adapter-fixed at 0 *because the adapter could not tell tasks apart*. It can
now, so the reason has moved to the profile table and the fixed 0 has become a per-profile default.

**Resolution does NOT run at the call site, and this was the one real design decision.** The obvious
shape — `complete_once` resolves the profile and hands the adapter a `ResolvedProfile` — is wrong
here, and provably so: every pipeline test injects `FakeLLM` with `llm_provider=None` and
`llm_model=""`, so a call site that resolved would raise `LLMConfigurationError` on the first
scripted judgement and take the whole suite with it. The call site therefore passes the name
UNRESOLVED. `resolve_profile()` is the function item 76's adapter calls; today it is tested directly
and nothing else invokes it.

**Cost of the alternative:** every test would have to configure a provider and a model it never uses,
to satisfy a resolution whose answer is then thrown away by a fake. That is a fixture change in every
suite to buy nothing.

**THE FALLBACK RULE.** A profile name that is not in `llm_profiles` resolves to the
`llm_provider` / `llm_model` pair at temperature 0 with no ceiling of its own. So:

| Deployment | What it configures |
| --- | --- |
| one model everywhere | `DODEAL_LLM_PROVIDER` + `DODEAL_LLM_MODEL`, no profiles at all |
| one pass moved | the pair, plus the one profile that differs |
| every pass pinned | three profiles; the pair becomes unused |

An unknown name with **no** pair configured raises `LLMConfigurationError` — the existing no-argument
exception, so the message stays the fixed `llm_not_configured` the factory already reports. It is the
same fault said in the same words: the seam was asked for a model without being told which one.

**THE CEILING RULE, in one sentence: a profile may LOWER a task's ceiling, never raise it.**

`CLASSIFY_MAX_OUTPUT_TOKENS = 64`, `VAGUE_MAX_OUTPUT_TOKENS = 1024` and `SCORE_MAX_OUTPUT_TOKENS = 256`
are unchanged and are still the numbers the call sites pass. `ResolvedProfile.effective_max_output_tokens(task_ceiling)`
returns `min(task_ceiling, profile_ceiling)` when the profile sets one and `task_ceiling` when it does
not. The result is the **effective ceiling**, and it is what a `MAX_TOKENS` finish reason is judged
against in `llm_call.parse_output` — so lowering a ceiling makes truncation *more* likely and is a
deliberate cap, while raising one is refused because the task constant is already sized against the
longest answer that task can produce (the Arabic sizing note in `vague.py` is the reason the 1024
exists at all). A profile that could raise it would be buying room the task has no use for.

**Both attempts of a reprompt carry the same profile.** The second call is the same task said more
strictly, not a different one; switching models between them would make the reprompt a second
variable and the malformed-answer diagnosis worthless.

---

### What changed

| File | What |
| --- | --- |
| `src/dodeal_ai/core/config.py` | `ModelProfile` (frozen `BaseModel`: provider, `model` with `min_length=1`, `temperature` `ge=0 le=1`, optional `max_output_tokens`); `llm_profiles: dict[str, ModelProfile]` read from `DODEAL_LLM_PROFILES` as JSON; `_build_settings` now also catches `SettingsError`. |
| `src/dodeal_ai/core/llm/profiles.py` | **New.** The three names, `KNOWN_PROFILES`, `ResolvedProfile` with `effective_max_output_tokens`, and `resolve_profile`. |
| `src/dodeal_ai/core/llm/client.py` | `profile: str` as a required keyword on `LLMClient.complete`, three-line docstring; the module docstring's temperature bullet becomes a provider/model/temperature bullet pointing at the profile table. |
| `src/dodeal_ai/core/llm/__init__.py` | One comment: the adapter that lands in item 76 owns the profile table and stays behind the parameterless factory. No signature change. |
| `src/dodeal_ai/units/structured_intelligence/llm_call.py` | `profile` threaded through `call_model` and `complete_once` as a required keyword, and onto the one `client.complete()` call — which serves both the first attempt and the reprompt. |
| `.../classify.py`, `.../vague.py`, `.../scoring.py` | One import and one keyword each: its own constant. Ceilings untouched. |
| `tests/helpers/fake_llm.py` | `RecordedCall.profile`, a `profiles` view, and `complete` takes the keyword. Still satisfies `LLMClient` for mypy and at runtime. |
| `tests/unit/test_model_profiles.py` | **New**, 25 tests: resolution, the fallback rule in four shapes, five construction-time validation failures, the ceiling rule in both directions, and the grep over `src/dodeal_ai/units/`. |
| `tests/unit/test_judgement_pipeline.py` | 3 added: the profile paired with its TEMPLATE, the reprompt on the same profile, each pass sending its own ceiling. `_OnePassHeld.complete` takes the keyword. |
| `tests/unit/test_llm_seam.py` | 2 added: `profile` is keyword-only, required and a `str`; the Protocol still has exactly one method. `_StructuralClient` updated. |
| `tests/helpers/test_fake_llm.py` | The recorded profile asserted alongside the recorded ceiling; the direct `complete()` calls name a profile. |
| `README.md`, `ASSUMPTIONS.md` §8.4, `docs/STATUS.md` | The env var with a JSON example, the fallback rule, the ceiling rule, the new file, the new test module. |

**The pairing test asserts profile against TEMPLATE, not against arrival order.** Vague detection and
scoring are gathered and either may reach the fake first; a test that indexed `calls[1]` would be
re-pinning exactly the scheduling accident `script_for` (Phase I.2) exists to remove. It builds
`{stable text -> expected profile}` and compares the map, so the assertion is order-free by
construction.

**The grep test has teeth.** `test_the_unit_names_a_profile_at_all` fails closed if the pattern stops
matching, so the KNOWN_PROFILES assertion cannot pass by searching an empty set — the same guard
`test_no_sync_clients.py` uses. `llm_call.py`'s `profile=profile` pass-through is the one allowed
non-constant and is named explicitly in the test rather than tolerated by a loose pattern.

---

### Decisions taken here, and their cost

1. **`_build_settings` now catches `SettingsError` as well as `ValidationError`.** Verified, not
   assumed: pydantic-settings raises `SettingsError` (a `ValueError`, **not** a `ValidationError`)
   when a complex field's env value will not parse as JSON, so a malformed `DODEAL_LLM_PROFILES`
   would have escaped the fail-closed conversion entirely. **This was already true of
   `DODEAL_DD_API_KEYS`** and has been true since that field landed — a malformed key map raised a
   bare `SettingsError` instead of `ConfigError`. The spec for this piece requires malformed JSON to
   be a `ConfigError`, and the same three lines fix both. **Cost of the alternative** (catching it
   only for profiles, e.g. with a validator): two different failure shapes for the same class of
   typo, in the same file.
2. **`LLMConfigurationError` was not given a message argument.** It is documented as "THE one
   definition" with a fixed message, and the resolution failure IS the factory's failure — no
   provider and no model. **Cost of the alternative:** a second message shape on an exception whose
   whole design is that `str()` is fixed and log-safe.
3. **`profiles.py` is not re-exported from `core/llm/__init__.py`.** One import path per name. The
   package `__all__` is the seam's own surface and a profile name is not part of the seam; the
   conservative reading of "one method, one factory" is to leave that surface alone. Trivially
   reversible if the adapter makes it awkward.
4. **`ResolvedProfile` is a frozen dataclass and `ModelProfile` is a pydantic model.** They look
   redundant. They are not: `ModelProfile` is the CONFIG shape, validated at construction and
   optional in every field the fallback supplies; `ResolvedProfile` is the RESOLVED shape, where
   every field has an answer. Collapsing them would mean the adapter receiving a type whose
   `temperature` might be unset. **Cost of the alternative:** the fallback rule becomes four
   `or`-defaults at the adapter, in item 76, where nothing tests it yet.
5. **The ceiling rule is a method on `ResolvedProfile`, not a branch in `complete_once`.** It is a
   statement about a profile, it is pure, and it is testable without an adapter or a model call.
   **Cost of the alternative:** the rule lives on the call path, where the only way to test it is to
   configure settings that the injected-fake tests deliberately do not have.

---

### For the lead

1. **`.env.example` was not touched** (campaign §0.8 forbids it), so `DODEAL_LLM_PROFILES` is
   documented in the README configuration table and nowhere else. It is optional and defaults to an
   empty map, so nothing breaks — but if you want it discoverable in the example file, that is one
   line and it is yours to add, along with whatever that file's outstanding unstaged change is.
2. **The `dd_api_keys` fail-closed gap was real and is now closed as a side effect.** A malformed
   `DODEAL_DD_API_KEYS` used to raise a bare `SettingsError` past the `ConfigError` conversion. No
   test covered it and none has been added for it here — decision 1 above explains why the fix
   landed in this piece, but the missing test for that field is not this piece's to add.
3. **Nothing configures a profile anywhere, deliberately.** No profile appears in any settings
   fixture, no adapter reads one, and `resolve_profile` has exactly one caller: its own test module.
   The three call sites name a profile that resolves to the fallback pair in every environment that
   exists today. **This is the piece working as specified** — item 76 is what makes it load-bearing —
   but it does mean the resolution path has never run under a real judgement.
4. **`ModelProfile.temperature` is bounded 0–1 because that is what the spec says, and providers do
   not agree on that range.** Anthropic's is 0–1; some others accept up to 2. If a second provider
   joins `LLMProvider`, the bound is either per-provider or the loosest of them, and that is a
   decision, not a widening.
5. **A profile can name a provider the deployment has no credentials for**, and nothing catches it
   until the adapter tries. Settings validate that the provider is a known `LLMProvider` member, not
   that it is usable. The credential field itself lands with the adapter in item 76; pairing the two
   checks is that piece's job.
6. **Three names, and nothing enforces the namespace but a test.** `KNOWN_PROFILES` is a flat tuple
   and `unit_a.` is a convention held by one assertion. When Unit B gets profiles, the question is
   whether the vocabulary stays one tuple or becomes per-unit — worth deciding before there are six
   names rather than after.
7. **The `profile` keyword is required, so every future fake or adapter must accept it.** That is the
   point, and it is also the one thing that will break an out-of-tree implementation of `LLMClient`
   if one exists anywhere. None does in this repo — `FakeLLM`, `_StructuralClient` and `_OnePassHeld`
   are all of them, and all three were updated.

**Nothing in this piece has been verified against a real model or a real provider.** Nothing is marked
`[V]` (ASSUMPTIONS §8.11). The profile table has never resolved to anything but a test fixture.
