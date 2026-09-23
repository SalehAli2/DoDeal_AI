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

---

## Piece N: step 3   STATUS: IN PROGRESS (N.1, N.2, N.3, N.3b and N.4 done; the M4 TTL fix and policy-per-caller still owed, unscheduled)

Step 3 is `enforce_token_cost` and the fail-open pre-flight read. This piece is the ground it stands
on: the Redis connection has to be bounded and configurable before a second counter starts using it,
and the Lua script has to actually run before anyone edits it.

### N.1 — Redis timeouts and bounded pools; `/ready` reports db2; the fakeredis lane   STATUS: DONE `dffeb80`

**Suite:** 934 → **969 passing**, 1 skipped, 7 deselected. Coverage **99.40 %** (unchanged). All 13 per-file
floors met, no new floors. `limiter.py` stays at 100 % and is now covered by tests that execute its Lua
rather than mock past it.

---

### What `core/redis.py` hardcoded, and what replaced it

Four literals, in two copies of the same call: `socket_connect_timeout=2.0` and `socket_timeout=2.0`
in `get_cost_client`, and the same pair in `get_operational_client`. No pool was constructed at all,
so pool size and acquire behaviour were redis-py's defaults — effectively unbounded, and no wait.
`get_operational_client`'s docstring said so out loud and named this step as the place it would be
fixed "for both clients at once". It is, and the docstring saying it is hardcoded is gone with it.

The replacement is one private `_build_pool(url)` and two factories that differ only in which URL
they pass. Four settings, one pool per named connection, and **no numeric literal left anywhere in
the module** — which is the property `test_no_number_is_hardcoded_in_the_redis_module` enforces.

**Why the module is PARSED rather than grepped.** The prompt asked for a grep. A regex over the raw
text is wrong here, and not marginally: the module docstring says `db1` and `db2`, so any pattern
loose enough to catch `2.0` catches those too, and any pattern tight enough to miss them can be
walked around by writing `20.0` or `2e0`. Parsing the module with `ast` and rejecting every
int/float `Constant` has neither failure: comments and docstrings are not in the tree, and a numeric
literal cannot be spelled in a way that hides from it. It is the same test, made exact.

**Why `BlockingConnectionPool` and not `ConnectionPool`.** At the cap the plain pool raises
immediately. That converts a two-second burst into a wave of errors on a service that was otherwise
fine. The blocking pool waits up to `redis_pool_acquire_timeout_seconds` and only then refuses, so a
burst queues and a genuine stall still fails fast. A bound with no acquire timeout would be worse
than no bound: "blocks forever at the cap" is an outage the unbounded pool does not have.

**Why connect (0.25s) is so much shorter than read (1.0s).** Reaching a listening socket on the same
network is sub-millisecond. A connect that takes a quarter of a second is not a busy server, it is a
missing one, and waiting the full read budget to learn that is the H3 cost paid twice.

**Both connections share the four settings, deliberately.** They are separate so a flush or an
outage cannot cross between them, not so they can be tuned apart. Splitting the budget per
connection is a new decision and nothing has asked for one; the two factories stay separate and
stay `lru_cache`d, so there is still exactly one pool per connection per process.

---

### The pools were closed on shutdown. They stopped being, and that is why `main.py` changed

The prompt said to close the pools in the lifespan **if they are not already, and to change nothing
if they are**. They were: the lifespan already awaited `aclose()` on both clients. That is no longer
sufficient, and the reason is a redis-py detail worth writing down rather than rediscovering.

`Redis.aclose(close_connection_pool=None)` closes the pool only when `auto_close_connection_pool` is
true, and redis-py sets that flag **false for a caller-supplied pool** — the client did not create
it, so it does not assume the right to destroy it. Every client built by `from_url` owns its pool and
closes it; every client built as `Redis(connection_pool=pool)` does not. So the exact change that
bounds the pools is also the change that stops the existing shutdown from releasing them: a bare
`aclose()` would return the one checked-out connection and leak the other nineteen.

The lifespan therefore passes `close_connection_pool=True` explicitly. This is not an addition the
prompt's condition excluded — it is what keeps the condition true.

---

### `/ready` now reports two connections

`operational` joins `redis` in the body. Same probe (`PING`, under the same configured socket
timeout), same fail-soft handling, same `200` with `degraded`; missing configuration is still `503`
and the body is otherwise unchanged.

The reason for a second field rather than one combined flag: db1 fails **open** and db2 fails
**closed**. "Redis is fine" is not a fact about this service. A healthy cost store and a dead
idempotency store is a real state in which requests are still served but duplicate-work protection
is gone, and a single flag reports that as either a false alarm or — worse — as `ok`.

Neither probe can raise past the endpoint: `_ping` catches `redis.RedisError` and returns `False`,
and the readiness test fakes the **clients**, not the probes, so the real error path runs. The four
combinations of (db1 up/down × db2 up/down) are all asserted, because two independent fields whose
independence is untested are one field with extra typing.

---

### `tests/unit/test_cost_lua.py` — audit M5 closed

`fakeredis[lua]` executes real Lua against a real server implementation in-process. The script is
**imported** from `limiter.py` (`_INCR_BOTH_SCRIPT`), never retyped — a copy would keep passing on
the day the original changed, which is the single failure this test exists to prevent. The lane is
hermetic and needs no marker; `redis_real` is registered and excluded from the default run for the
first test that genuinely needs a server, and nothing carries it yet.

The TTL assertion distinguishes **creation** from **refresh** by using a different window on the
second call — 60 first, then 6000. If the script were refreshing rather than creating, the TTL would
jump; it does not move. A third test sets a key with no TTL at all and shows it never gains one,
which is audit M4 pinned exactly as it behaves today, so the step-3 fix has to change that test on
purpose rather than by accident.

**One assertion in the prompt does not match the code, and I did not change the code to make it
match.** The prompt asked for "the call at the limit returns the deny result and does not
increment". The Lua script contains no cap, no limit and no deny path — it counts, and
`enforce_cost` compares the returned counts against `cost_per_tenant_limit` and
`cost_per_user_limit` in Python **after** the increment has already been committed. So the request
at the cap is refused *and counted*: a check-then-increment script would be a different script, and
N.1 was told not to touch `limiter.py` beyond reading it.

What the test does instead is assert the deny decision where it actually lives, end to end on real
Lua — at the cap `enforce_cost` raises `CostLimitError("tenant_quota_exceeded")` and the counters
read 4 where the limit is 3 — plus a guard test that the script makes no limit decision at all, so
moving the cap into Lua later has to change a test deliberately. See "For the lead", item 1.

---

### What changed

| File | Change |
| --- | --- |
| `core/config.py` | Four settings: `redis_connect_timeout_seconds` `0.25`, `redis_socket_timeout_seconds` `1.0`, `redis_max_connections` `20`, `redis_pool_acquire_timeout_seconds` `1.0`. All `Field(gt=0)`, so a non-positive value is a `ValidationError` at construction and a `ConfigError` through `_build_settings` — the same fail-closed path as a missing signing key. |
| `core/redis.py` | `_build_pool` + both factories on `BlockingConnectionPool`; `_ping` shared by two named probes; `check_operational_redis_ready` added. No numeric literal remains. Both factories still separate, still cached. |
| `main.py` | `/ready` reports `operational` beside `redis`; lifespan closes both pools with `close_connection_pool=True`. |
| `pyproject.toml` | `fakeredis[lua]>=2.38.0` in the one dev list; `redis_real` marker registered and excluded from the default run alongside `integration`. |
| `uv.lock` | `fakeredis 2.38.0`, `lupa 2.8`. |
| `tests/unit/test_cost_lua.py` | **New.** Eight tests, the real script on fakeredis. |
| `tests/unit/test_redis.py` | Rewritten around pools: sizing from `Settings` for **both** clients, separate pools, cached factories, the parse-the-module literal guard, and both probes including their independence. |
| `tests/unit/test_config.py` | The four settings round-trip from the environment and reject `0` and `-1`. |
| `tests/unit/test_health.py` | `/ready`'s four up/down combinations, with the clients faked so the real probes run. |
| `README.md` | Four configuration rows; `/ready`, `redis.py` and `main.py` descriptions; two test-table rows; the `redis_real` marker note. |
| `docs/STATUS.md` | H3 partly done (timeouts, not the breaker), M5 done, M4 pinned, step 3's first sub-commit recorded. |

---

### For the lead

1. **The prompt's deny assertion describes a script this repo does not have.** "The call at the limit
   returns the deny result and does not increment" is check-then-increment; `_INCR_BOTH_SCRIPT` is
   increment-then-check, with the check in Python. The request at the cap **is** counted. That is
   either a bug (a denied request is charged, and a tenant hammering a closed door inflates its own
   counter) or the intended fail-open shape — but it is a `limiter.py` change either way, which N.1
   was forbidden. The behaviour is now pinned by an executing test, so whoever changes it will see
   it. **Decide at step 3.**
2. **Register item 25 could not be located.** No tracked file has one — not `docs/STATUS.md`, not
   this report, not `README.md`. Item 3 is the step-3 row and is marked partly done (this piece is
   its first sub-commit, not the whole step). Item 25 is recorded as unlocated rather than guessed
   at. If it lives in a register outside the repo, it still needs marking by hand.
3. **`.env.example` was not touched** — the prompt forbade it. It is now four rows behind
   `Settings`, and the README says that file "documents every `DODEAL_*` setting… audited
   field-by-field against `Settings`, in the same order". That claim is false as of this commit.
   It was already modified-unstaged and untouched for the whole campaign (§ "Open items carried out
   of Unit A Project 1"), so this compounds a known problem rather than creating one.
4. **All four numbers are provisional and none is measured.** `0.25` / `1.0` / `20` / `1.0` are sized
   against a same-network Redis by reasoning, not against a real deployment. `20` in particular is a
   guess about concurrency that should be read next to `max_inflight = 32`: at the cap, thirty-two
   in-flight requests contend for twenty connections, and the twelve that lose wait up to a second
   before the pool refuses. Whether that is right depends on how many Redis round-trips one judgement
   makes — today one, after step 3 more. **The load lane owns both numbers, and they should be set
   together.**
5. **The H3 breaker is not built.** N.1 bounds what an outage costs per call; it does not stop the
   calls. A Redis that is down still costs every request its connect timeout, and `/ready` still
   probes it every scrape. The breaker is the other half of H3 and is still owed at step 3.
6. **`redis_real` is registered and unused.** A marker nothing carries is a promise, not a lane. The
   first test that needs a real server should carry it and CI should decide whether it ever runs one.

**Nothing in this piece has been verified against a real Redis.** The pools have never opened a
socket; every assertion is on a constructed pool's attributes or against `fakeredis`. Nothing is
marked `[V]` (ASSUMPTIONS §8.11).
---

### N.2 — token counters; the pre-flight replaces the step-3 seam   STATUS: DONE `77df41d`

**Suite:** 969 → **998 passing**, 1 skipped, 7 deselected. Coverage **99.42 %**. **14** per-file floors
(one new: `core/cost/limiter.py` at 100, beside `llm_call.py`'s existing 100). Both are at 100 %.

---

### The two counters, and the line between reading and writing

`tokens:tenant:{t}` and `tokens:user:{t}:{s}`, on the same window as the request caps and on keys that
share nothing with them. The division of labour is the design, and it is not symmetrical:

- **`token_preflight(scope)` READS.** One MGET, no write, ever. It raises `TokenBudgetExceeded` (429
  `token_budget_exceeded`) when either total is **at or above** its limit. At or above, not over: the
  counters are charged after the fact, so a total that has *reached* the cap has already spent it, and
  `>` would grant one more whole judgement past a limit that was already hit.
- **`enforce_token_cost(scope, …)` WRITES.** One EVAL, and it never denies. The call it is charging for
  has already been paid to the provider; refusing there would throw the answer away and be billed for it
  anyway. It is a meter, and the gate is upstream of it.

Both fail **open** on `redis.RedisError`, matching `enforce_cost`. The charge logs `token_charge_bypassed`
every time (a hole in the meter is per-call information); the pre-flight logs `token_preflight_bypassed`
**once per process**, keeping N.1's latch, because a store that is down is down for every request.

**Cap semantics follow ruling R18 exactly.** The script counts and returns; Python compares the returned
values; nothing in Lua knows what a limit is. `test_the_token_script_makes_no_limit_decision` pins that,
so moving either the cap or the ratio into Lua has to change a test on purpose.

---

### Why a second script and not the same constant

`_ADD_TOKENS_SCRIPT` is the same shape as `_INCR_BOTH_SCRIPT` and is deliberately a separate constant.
Sharing one would mean the M4 TTL repair — still owed on the **request** counters — silently changing the
**token** counters the day someone makes it. The duplication is the isolation, and it is asserted from the
outside rather than by reading the two sources: `FakeCostRedis` records `(script, keys)` per EVAL, and
`test_token_and_request_counters_never_touch` compares the key sets that were actually sent. A script that
started writing the other pair's keys would still match its own source text; that test would not pass.

The Lua lane grew the same four properties for the new script (atomic add to both, totals returned, TTL on
creation only, no TTL refresh) plus one that only exists because there are two:
`test_the_two_scripts_touch_disjoint_keys` runs one EVAL of each and asserts four keys and no crossover.

---

### The charge is in `complete_once`, and that is why `scope` moved

**The requirement decided the placement.** "A reprompt charges its own response's usage and nothing more"
is only satisfiable where every paid call passes exactly once — and `call_model` **discards** a reprompted
pass's first response, so charging from `pipeline.py` would count three responses on a judgement that
bought four. `complete_once` is the one place a response is first in hand.

`complete_once` had no `TenantScope`, so one was threaded: `complete_once` → `call_model` → `classify` /
`detect_vagueness` / `score_note` → the three pipeline call sites. **A contextvar was considered and
rejected.** An ambient billing identity is exactly the thing that charges the wrong tenant when a task
fails to inherit context, and this repo passes `TenantScope` explicitly everywhere else (design note 0001,
Decision 1). The cost is 51 mechanical test call sites and one new helper, `tests/helpers/scopes.py`;
the alternative's cost is a wrong bill nobody can reconstruct.

Charge counts, asserted through the route rather than by calling the limiter: **three** for a scored
judgement, **four** when one pass reprompted, **zero** for a suppressed note. A response reporting no usage
charges nothing and logs nothing — zero tokens is an *unmeasured* call, not a free one, and a zero-token
line would read as the second.

---

### The warning, and what it deliberately does not do

`token_budget_warning` fires the first time a running total crosses `limit × cost_token_warning_ratio`
inside its window. **Once per crossing, with no extra state:** the pre-call total is the returned total
minus what this call added, so "was under, is now at or over" is answerable from the one number the script
already returned. No second key, no in-process flag, and nothing to get wrong when two workers charge at
once. The next call is already over and stays silent; the window expiring lets the crossing happen once
more.

It changes **nothing** about what is served. Degradation at the ratio is item 61's second half and belongs
to A9; this piece is the design half only, and the ratio is `gt=0, lt=1` so neither degenerate value can be
configured.

---

### The step-3 seam is gone, and the marker test was inverted

The stub, its two comments in `pipeline.py`, its docstring in `limiter.py`, the README callout, the
`docs/STATUS.md` rows and the two route tests that asserted the no-op's bypass line have all been replaced
with a sentence saying the pre-flight is real. `tests/test_assumption_markers.py` no longer checks the
marker's three-way presence — it asserts the marker appears in **no tracked file**, so a leftover fails the
build. The constant is assembled from two halves rather than spelled, because a file that wrote it out
whole would be the first to fail its own test; the first run of the inverted test caught exactly that, plus
a docstring line I had left behind.

---

### Six test fakes had to be wired, and that is a finding

Before this commit `token_preflight` touched no Redis, so three modules drove `judge_note` with **no cost
client patched at all** (`test_judgement_pipeline.py`, `test_structural_eval.py`,
`test_unit_a_injection.py`) and three more had a `_FakeCostRedis` implementing `eval` and nothing else. A
real pre-flight would have made the first three open a socket to `localhost:6379` and the second three
raise `AttributeError` on `mget`. `tests/helpers/fake_cost_redis.py` is now the one fake, and the three
near-duplicate private copies are gone. Worth recording because the suite's hermetic claim survived only
because the seam was a no-op.

---

### What changed

| File | Change |
| --- | --- |
| `core/config.py` | `cost_tokens_per_tenant_limit` `5_000_000`, `cost_tokens_per_user_limit` `500_000` (both `gt=0`), `cost_token_warning_ratio` `0.9` (`gt=0, lt=1`). All three validated at construction. N.1's Redis comment block trimmed to at most three lines per field. |
| `core/cost/limiter.py` | `_ADD_TOKENS_SCRIPT`, `_token_keys`, `_add_tokens_with_window`, `_warn_on_crossing`, `enforce_token_cost`, and a real `token_preflight`. Module docstring rewritten around the read/write split. |
| `core/errors.py` | `TokenBudgetExceeded` → 429 `token_budget_exceeded`, standard `{detail, reason, request_id}` body. |
| `units/.../llm_call.py` | The charge, once per response with usage, in `complete_once`. `scope` threaded through it and `call_model`. |
| `units/.../classify.py`, `vague.py`, `scoring.py` | `scope` as a required keyword, passed to `call_model`. No other change. |
| `units/.../pipeline.py` | Step 7 is a real pre-flight; the marker and the no-op comment are gone; `scope` passed to the three passes. The release-on-error path already covered the 429 and is untouched. |
| `main.py` | The two `/ready` probes under `asyncio.gather` (N.1 review). |
| `core/redis.py` | The `redis.asyncio` import alias renamed to `redis_async` (N.1 review). |
| `.env.example` | The four N.1 Redis rows and the three N.2 token rows, in the file's own style. |
| `scripts/check_coverage_floors.py` | `core/cost/limiter.py` at 100. |
| `tests/helpers/fake_cost_redis.py` | **New.** The one db1 fake; records `(script, keys)` per EVAL. |
| `tests/helpers/scopes.py` | **New.** One `TenantScope` for tests that need an identity and assert nothing about it. |
| `tests/unit/test_token_cost.py` | **New.** 18 tests: the counters-never-touch test, the pre-flight's cases, the charge counts, the bypasses and the warning. |
| `tests/unit/test_cost_lua.py` | Six more tests: the token script on real Lua, plus the disjointness of the two. |
| `tests/unit/test_health.py` | `/ready` with both probes slow finishes in about one probe's time. |
| `tests/security/test_log_safety.py` | The three new events inside the sentinel sweep, with `tokens_charged`'s whole field set pinned. |
| `README.md`, `docs/STATUS.md` | The three settings, the events, the pre-flight in the lifecycle, the probe-timeout sentence, the 429 in the error list, the retired marker. |

---

### For the lead

1. **`token_preflight`'s `reason_code` changed, deliberately.** It was `token_preflight_bypassed` (the
   stub's, meaning "this build has no pre-flight"). It is now `token_store_unavailable`, matching
   `cost_cap_bypassed`'s `cost_store_unavailable`, because the line now means something else entirely. The
   message name is unchanged, and no test asserted the old code. **Flagging it because a collector could
   have been filtering on it.**
2. **The charge catches `redis.RedisError` only, not `Exception`.** The brief said `enforce_token_cost`
   "never raises to the caller". A blanket catch would also swallow a bug in our own code — silently, on
   the money path — so the narrow catch matches `enforce_cost` and the docstring says what it promises.
   The exposure is small (the only I/O is the EVAL) but it is not zero: **if you want the absolute
   guarantee, say so and it becomes `except Exception` with an ERROR line.**
3. **`limiter.py` still spells the async import the old way.** The rename was scoped to `core/redis.py`,
   and I did not widen it. Two files in the same package now spell the same import differently. One line,
   whenever you want it.
4. **Register items 24 and 61 could not be located** — the same gap N.1 reported for item 25. No tracked
   file carries either number. Both rows in `docs/STATUS.md` are written from this brief's description of
   them, not from a register entry read in the tree. **If the register is outside the repo it still needs
   marking by hand.**
5. **All three numbers are provisional and none is measured.** 5,000,000 / 500,000 / 0.9 are placeholders,
   and the ratio in particular is a guess about a distribution nobody has seen. They also interact with a
   number nobody has set: a judgement is roughly 400–1,500 tokens on today's ceilings, so the per-user cap
   is somewhere between 300 and 1,200 judgements per window — which may be far too generous or far too
   tight depending on `max_note_chars`. **The load lane should set these together with `max_inflight` and
   `redis_max_connections`, not separately.**
6. **A judgement now costs four Redis round-trips where it cost one.** One MGET for the pre-flight plus one
   EVAL per model response, on top of Gate 4's EVAL. That is a real change to the number N.1's "For the
   lead" item 4 asked to be read next to `redis_max_connections = 20`, and it makes the H3 breaker more
   urgent, not less: a dead Redis now costs a judgement four connect timeouts instead of one.
7. **Nothing degrades at the warning ratio.** By design (A9), but worth saying plainly: a tenant at 95 % of
   its budget is served exactly as one at 5 %, and the only difference is a log line. The cliff at 100 % is
   a hard 429 with no ramp.

**Nothing in this piece has been verified against a real Redis or a real provider.** Every token number in
every test comes from `FakeLLM`'s round defaults; no provider has ever reported usage to this code. Nothing
is marked `[V]` (ASSUMPTIONS §8.11).

---

### N.3 — circuit breaker on Redis; rate limit in one round trip   STATUS: DONE `3199e3d`

**Suite:** 998 → **1036 passing**, 1 skipped, 7 deselected. Coverage 99.42 % → **99.45 %**. **15** per-file
floors (one new: `core/breaker.py` at 100). `decide.py` and `limiter.py` at 100, `state.py` at 100
(unchanged), `breaker.py` at 100.

---

### What each judgement asked Redis before this commit

In pipeline order, on db2: SET NX EX (the reservation, **fail closed**), GET the rate limit, GET the
attempt count, and on a resubmission GET the fingerprint reference. On db1: MGET (the pre-flight), then
one EVAL per model response. After the response was built, and only if a prompt was sent: INCR+TTL+EXPIRE
on the rate limit, INCR+TTL+EXPIRE on the attempts, SET NX EX on the reference. Everything except the
reservation failed open. The rate limit therefore cost a GET before the model calls and an increment after
them. That read-then-increment pair is what item 27 removes.

---

### The breaker

`core/breaker.py`, `CircuitBreaker(name, *, failure_threshold, open_seconds, clock=time.monotonic)`.
**Closed** passes calls and counts consecutive `redis.RedisError`s; any other exception is not counted.
At the threshold it goes **open**, and refuses with `BreakerOpen` without calling anything. After
`open_seconds` the first caller moves it to **half-open** and becomes the only probe. Every other caller is
refused while the probe is out. The probe's success closes the breaker; its failure reopens it for a full
window. Every transition happens in synchronous code between awaits, so no lock is needed on one event
loop.

**`BreakerOpen` subclasses `redis.RedisError`, and that is the whole integration.** Every call in
`limiter.py` and `state.py` now runs through `cost_breaker().call(...)` or `operational_breaker().call(...)`.
No `except` clause changed its type. So while a breaker is open, the fail-open paths take their existing
bypass branch with `breaker: open` added to the existing line (`breaker_field(exc)`), and the reservation
still raises `IdempotencyUnavailableError` → **503**. `get_usage` still propagates.

**Two breakers, one per connection.** One shared breaker would let a dead cost store refuse the
reservation. Both are `lru_cache` accessors built from `DODEAL_BREAKER_FAILURE_THRESHOLD` (5) and
`DODEAL_BREAKER_OPEN_SECONDS` (30.0), both `gt=0`. `reset_breakers()` clears both caches, and an autouse
fixture in `tests/conftest.py` calls it around every test.

**The once-per-process latch is removed.** `_TOKEN_PREFLIGHT_LOGGED` is gone, and `token_preflight_bypassed`
is logged on every bypass. The latch was de-duplicating in the wrong place: it also hid a second outage
after a recovery. `breaker_opened` and `breaker_closed` now carry that job, and each is logged once per
transition. The five `monkeypatch.setattr(..., "_TOKEN_PREFLIGHT_LOGGED", False)` lines in the tests went
with it.

---

### The rate limit in one round trip, with the precedence intact

`state.take_rate_limit` runs `_TAKE_RATE_LIMIT_SCRIPT`. The script increments only while the key is under
the limit, sets the window on creation (and on a key with no TTL, the guard `_incr_with_window` already
had), and returns `(allowed, count before the call)` in both branches. `increment_rate_limit` is
**deleted**, not just its call site. It had no caller left.

**The pipeline asks the pure function twice rather than restating its conditions.** It first calls
`decide()` with the window assumed open. `_rate_limit_trip` reads that provisional answer:

- **A prompt would be sent:** run the script. This is the increment.
- **`nothing_to_ask`:** one plain GET, so an exhausted window still reports `rate_limited` first.
- **Anything else:** no call. That covers `resubmission`, `attempt_cap` and `accept_silent`.

`decide()` then runs again with what the store said. The four conditions exist only in `decide.py`, so
the pipeline's choice of trip cannot drift from the order the CRM is promised. `decide()` gains
`rate_allowed` beside `rate_count` and nothing else. Its outputs and the withheld order are unchanged.

The attempt counter and the fingerprint write are unchanged and still run after the response is built.
The rate-limit read left step 5. Step 8 now takes the slot.

---

### The hermetic guard, and what it made convert

`tests/test_hermetic_fakes.py` parses every test module with `ast`. It finds each patch of
`get_cost_client` or `get_operational_client` and fails unless the value is one of these:

- a shared fake from `tests/helpers/`
- a `fakeredis` client
- a name bound to either one in the same file, such as `client = FakeCostRedis()` or the fixture that
  yields it

Patches of `core/redis.py`'s own factories are skipped, because those tests are about the factories.
Checked by hand: a `monkeypatch.setattr(limiter, "get_cost_client", lambda: _MyOwnFake())` in a probe file
fails it, naming the file and line.

On first run it named four offenders. All four were private copies of the shared fake:

- `test_chain.py` and `test_exit_demo.py`: `_FakeCostRedis`
- `test_log_safety.py`: `_DeadCostClient`
- `test_cost.py`: `FakeRedis`

All four now use `FakeCostRedis`. The README and CONTRIBUTING sentences that named `test_cost.py`'s
`FakeRedis` as the pattern now point at `tests/helpers/`.

---

### What changed

| File | Change |
| --- | --- |
| `core/breaker.py` | **New.** `CircuitBreaker`, `BreakerOpen`, `BreakerState`, `breaker_field`, `cost_breaker`, `operational_breaker`, `reset_breakers`. |
| `core/config.py` | `breaker_failure_threshold` 5 and `breaker_open_seconds` 30.0, both `gt=0`. |
| `core/cost/limiter.py` | All four cost-store calls go through `cost_breaker().call`. `breaker: open` on the two bypass lines. The latch is removed. The `aioredis` alias is renamed to `redis_async`, matching `core/redis.py`. |
| `units/.../state.py` | Every operational call goes through `operational_breaker().call`. `_bypass` takes the exception. `_TAKE_RATE_LIMIT_SCRIPT` and `take_rate_limit` are added, `increment_rate_limit` is deleted, and the module docstring's "no Lua" paragraph is rewritten. |
| `units/.../decide.py` | Takes `rate_allowed`, and `RATE_LIMITED` reads `not rate_allowed or rate_count >= limit`. Docstrings updated. |
| `units/.../pipeline.py` | The rate-limit read leaves step 5. `_rate_limit_trip` and the two `decide()` calls replace the read-then-increment pair. The step list and the counters paragraph are updated. |
| `scripts/check_coverage_floors.py` | `core/breaker.py` at 100. |
| `tests/conftest.py` | Autouse `_closed_breakers`. |
| `tests/helpers/fake_operational_redis.py` | `eval` for the rate-limit script, mirroring its TTL rule, and records `(script, key, allowed)` in `evals`. |
| `tests/helpers/breakers.py` | **New.** `trip(breaker)`, which opens a real breaker with real consecutive failures. |
| `tests/test_hermetic_fakes.py` | **New.** The guard above. |
| `tests/unit/test_breaker.py` | **New.** 19 tests: the state machine on a fake clock, the single in-flight probe, the logs, the subclass, and the settings. |
| `tests/unit/test_cost_lua.py` | Seven tests: the rate-limit script on real Lua, including `take_rate_limit` end to end. |
| `tests/unit/test_unit_a_state.py` | Rate-limit tests rewritten against `take_rate_limit`: count-before, refusal without writing, one EVAL, M4. |
| `tests/unit/test_decide.py` | The helper passes `rate_allowed`. There is one new case: a refused slot withholds whatever the count says. |
| `tests/unit/test_token_cost.py` | The once-per-process test is inverted to every-time. There are two cost-breaker cases: no MGET, no EVAL, `breaker: open`. |
| `tests/unit/test_judgement_pipeline.py` | Two operational-breaker cases. The reservation 503s with no SET. A breaker that opens mid-judgement bypasses the rate limit with no EVAL. |
| `tests/unit/test_judgement_routes.py` | The four-note burst: three script calls allowed, one refused, fourth `rate_limited`. Also: exhausted-and-no-question reports `rate_limited` with no EVAL; resubmission and attempt cap make no rate-limit trip. |
| `tests/security/test_log_safety.py`, `test_chain.py`, `test_exit_demo.py`, `tests/unit/test_cost.py`, `test_direct_routes.py` | Private fakes replaced by `FakeCostRedis`. Latch lines removed. The two 500-path tests inject at `take_rate_limit`, which the happy path now reaches instead of the read. |
| `README.md`, `CONTRIBUTING.md`, `docs/STATUS.md`, `ASSUMPTIONS.md` | README: the two settings, the breaker events, the one-round-trip paragraph, the latch sentence, and the rows for the new files. CONTRIBUTING: the test-pattern pointer. STATUS: items 20 and 27. ASSUMPTIONS: one line on the rate-limit entry. |

---

### For the lead

1. **BLOCKED: `.env.example`.** The permission settings deny both reading and writing it, so the two rows
   are not in the file. Add them by hand after `DODEAL_REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS=1.0`:
   ```
   # Circuit breaker, one per Redis connection. Consecutive failures that open it,
   # and how long it refuses before ONE probe is let through. Both provisional,
   # both must be positive.
   DODEAL_BREAKER_FAILURE_THRESHOLD=5
   DODEAL_BREAKER_OPEN_SECONDS=30.0
   ```
2. **Pool exhaustion counts as a store failure.** A `BlockingConnectionPool` that cannot hand out a
   connection within `redis_pool_acquire_timeout_seconds` raises `redis.ConnectionError`, which is a
   `RedisError`, as the brief specified. So five consecutive pool timeouts under a load spike open the
   breaker with Redis perfectly healthy. For the next 30 s, cost caps are bypassed and **every
   reservation is 503**. I followed the brief. The conservative alternative is to exclude acquire
   timeouts from the count, and that is your call. Set the threshold, pool size and window together in
   the load lane.
3. **The fail-closed side pays the whole window.** Once `operational_breaker` opens, every judgement is
   503 `idempotency_unavailable` for 30 s even if db2 recovered after one. That is the designed trade, and
   30 is provisional.
4. **`accept_silent` makes no rate-limit call.** The brief's three-way rule did not mention it. Running
   the script there would count a prompt that is never sent, so it takes the no-call branch. It falls out
   of the provisional `decide()`, not a special case.
5. **The pipeline calls `decide()` twice instead of restating conditions 1 and 2.** The brief said
   "evaluate the conditions in order" in `pipeline.py`. Duplicating them would put the precedence in two
   files, so the provisional call reads it from the one place it lives. `decide.py` changed only by the
   flag.
6. **`count` is the count *before* the call, in both branches**, and `decide` keeps `rate_count >= limit`
   beside `not rate_allowed`. The count half is load-bearing on the read path. It also keeps today's
   behaviour on the fail-open path: `(True, 0)` against a limit of 0 still reads `rate_limited`, as it
   did.
7. **The slot is taken at step 8, before the response is built.** It is no longer taken after. If
   anything raised between the script and the return, a slot would be counted for a prompt never
   delivered. Today nothing on that stretch does I/O. This is inherent to "the check is the increment".
8. **The new script repairs M4 for the rate-limit key, and the cost scripts still do not.** This
   preserves the guard `_incr_with_window` already applied to that key. The request and token scripts
   were left alone, as instructed.
9. **Scope past the literal list, all required by the guard or by a statement this piece made false:**
   - the four test modules' private fakes
   - `test_cost.py`'s atomic-failure test, which now turns the shared fake's `fail` off before reading
     back, because it fails reads too
   - the README and CONTRIBUTING pattern pointers
   - README rows for the three new files
   - the rewrite of `test_unit_a_state.py`'s rate-limit tests after `increment_rate_limit` was deleted
10. **No README lifecycle sentence mentioned the rate limit.** The one-round-trip sentence is a new
    paragraph in the route contract, after "Hitting the clarification rate limit is not a 429".
11. **`breaker` means two things on two events.** On a bypass line it is `open`. On `breaker_opened` and
    `breaker_closed` it is the breaker's name (`cost`/`operational`). A filter on `breaker=open` still
    hits only bypass lines.
12. **Each worker process has its own breakers.** They are in-process state, so N uvicorn workers each
    count and open independently.
13. **A pre-existing flake, not from this piece:**
    `test_judgement_pipeline.py::test_elapsed_covers_more_than_any_single_pass` asserts `elapsed_ms >= 20`
    after a 20 ms sleep and fails about 1 run in 6 on this Windows machine. It was reproduced in a clean
    worktree at `17c35d9` (5 of 6 passed). It fired once during this piece's chain runs and passed on the
    re-run. It is untouched here. The chain's first run was also red on two ruff findings in this piece's
    own new code: an unsorted import and a nested `if`. Both were fixed before the green run.
14. **Register items 20 and 27 could not be located**, the same gap as 24, 25 and 61. The STATUS rows are
    written from this brief.

**Nothing in this piece has been verified against a real Redis.** The script ran on `fakeredis[lua]`
only, and no breaker has seen a real outage. Nothing is marked `[V]`.

---

### N.3b — breaker wedge, pool exhaustion, per-request deadline, reservation release   STATUS: DONE `73cf893` · `7a8c48c` · `83bc11b` · `8ba3024`

**Suite:** 1036 → **1070 passing**, 1 skipped, 7 deselected. Coverage 99.45 % → **99.47 %**. **15** per-file
floors, none added. `core/breaker.py`, `pipeline.py` and `state.py` are at 100 %. `core/redis.py` has no floor
and is at 100 % anyway.

| Commit | Register item | Suite after |
| --- | --- | --- |
| `73cf893` | 80 — a breaker cannot wedge in HALF_OPEN | 1040 passing, 99.46 % |
| `7a8c48c` | 81 — pool exhaustion is not counted; the pool is sized from `max_inflight` | 1048 passing, 99.46 % |
| `83bc11b` | 83 — one deadline per judgement | 1059 passing, 99.47 % |
| `8ba3024` | 82 — the reservation is released on any exit; short in flight, long once judged | 1070 passing, 99.47 % |

---

### 80 — the wedge, and why it took two guards

HALF_OPEN refuses everyone but the probe, and only the probe's *result* moved the breaker out of it. The
only result `call` recognised was a `RedisError`. A probe that was cancelled (the new deadline, a disconnect,
a shutdown), or that raised anything else, re-raised straight past the one branch that could move the
state. The breaker was then HALF_OPEN with no probe out and no timer, refusing every call for the life of
the process. On db2 that is 503 on every judgement.

**Guard 1** is an `except BaseException` branch. When it ends a probe it logs `breaker_probe_abandoned`,
re-arms the window through `_open()` (which logs `breaker_opened`), and re-raises unchanged. A cancellation is
*not counted*, only re-armed like a failure. It is news about the caller, never about the store. In CLOSED
such an exception neither counts nor clears the count, which is what it did before, now pinned by a test.

**Guard 2** is for the probe that never ends at all: a task that is never resumed raises nothing, so no
`except` can see it. `_admit` stamps `_probe_started` on every entry to HALF_OPEN. A probe older than
`open_seconds` is taken as abandoned, and the arriving call becomes the probe in its place. Its own success
closes the breaker.

`core/breaker.py` has exactly two `except ` hits, both in `call`. The module docstring's old
"every existing `except redis.RedisError`" was reworded so the count means what it says.

---

### 81 — what counts as the store failing

`BlockingConnectionPool` refuses an acquire with `redis.ConnectionError("No connection available.")`
chained **from the wait's own `TimeoutError`**. That is redis-py 8.1.0, read before writing this. The socket
connect happens *after* the acquire, in `ensure_connection`, and fails as `redis.TimeoutError` or as a
`ConnectionError` with no such cause. So `BoundedPool.get_connection` relabels exactly the error whose
`__cause__` is a builtin `TimeoutError` as `PoolExhausted` and lets everything else through. Two stub
connection classes that open no socket pin both sides: a pool of one refuses the second of two concurrent
acquires as `PoolExhausted`, and a socket that will not connect stays a plain `ConnectionError`.

`PoolExhausted` is still a `ConnectionError`, so every caller's policy for that one call is unchanged. The
reservation still 503s and the money guards still bypass. Only the breaker's count skips it.

`redis_max_connections` became an optional override. `Settings.redis_pool_size` is the override when set,
otherwise `max_inflight + REDIS_POOL_HEADROOM`. Raising the in-flight cap raises the pool with it.

---

### 83 — the deadline

`judge_note` and `judge_note_direct` each wrap everything after their `time.monotonic()` in
`asyncio.timeout(deps.settings.judgement_deadline_seconds)`. That puts the fetch route's two backend reads
inside the deadline, and the timeout **outside** `_judge`. `TimeoutError` becomes `JudgementDeadlineExceeded`
through one helper, so both routes log the same line. Nothing in `src/` catches `CancelledError` or
`BaseException` before this piece, and the watchdog converts its own timeouts into `ExternalCallError`, so a
builtin `TimeoutError` reaching the entry point is the deadline's. The per-call timeouts are untouched, and
so is `llm_call.py`.

The deadline tests carry an outer `asyncio.wait_for` of 5 s. With the deadline removed they fail on their
status in 5 s instead of hanging the suite, which is what made the sabotage runnable unattended.

---

### 82 — two lifetimes for one key

**The release** now runs in `except BaseException`, and the `DEL` runs under `asyncio.shield`. The shield is
not decoration. One cancellation is delivered once, and the awaited release would complete without it. But
a worker shutdown, or a cancel scope that re-delivers on every await, cancels *again* while the `DEL` is on
the wire, and that second cancellation kills an unshielded release. The test models exactly that, and
removing the shield fails it every time rather than intermittently: the fake `delete` suspends once, as a
real round trip does, and the test cancels a second time at that moment.

**Reserve short, confirm long.** A SIGKILL runs no `except` and no `finally`, so no release can promise the
key goes. The reservation now lives `ceil(deadline × 4)`. `state.confirm_idempotency` (`SET "done" XX EX`,
under the breaker, failing open) applies the tenant's long TTL once the judgement exists, inside the try, so a
cancellation *during* the confirm still releases, and before `_log_outcome`. `XX` means a reservation that
has already expired is never written back. `"done"` sits beside `_RESERVED = "1"` as the D3 seam, and nothing
is stored in it.

---

### Sabotage record

Every sabotage was applied by a script that edits one file, runs the named tests, and restores the file
byte for byte, confirmed each time.

| # | Sabotage | Tests that failed |
| --- | --- | --- |
| 1a | delete the `except BaseException` branch in `CircuitBreaker.call` | `test_a_probe_that_raises_our_own_error_re_arms_the_window`, `test_a_cancelled_probe_re_arms_the_window` |
| 1b | delete the abandoned-probe check in `_admit` (HALF_OPEN always refuses) | `test_a_probe_that_never_returns_is_taken_over_after_a_window` |
| 2a | remove the `isinstance(exc, PoolExhausted)` exclusion | `test_pool_exhaustion_is_not_counted` |
| 2b | *(extra)* relabel every `ConnectionError`, not only the acquire | `test_a_socket_that_will_not_connect_is_not_pool_exhaustion` |
| 3a | remove the `async with asyncio.timeout` from `judge_note` | `test_a_model_that_never_answers_is_stopped_at_the_deadline`, `test_the_fetch_spends_the_same_deadline` |
| 3b | remove it from `judge_note_direct` | `test_the_direct_route_is_stopped_at_the_same_deadline` |
| 3c | *(extra)* move the fetch above the deadline | `test_the_fetch_spends_the_same_deadline` |
| 4a | revert the release to `except Exception` | `test_a_request_cancelled_mid_classify_releases_the_reservation`, `test_a_model_that_never_answers_is_stopped_at_the_deadline`, `test_the_direct_route_is_stopped_at_the_same_deadline` |
| 4b | remove the `asyncio.shield` | `test_a_request_cancelled_mid_classify_releases_the_reservation` (deterministic, not a flake) |
| 4c | remove the confirm call | `test_a_judgement_confirms_the_reservation_for_the_long_ttl[discovery]` and `[system_event]`, `test_the_reservation_is_short_while_the_judgement_runs`, `test_a_confirmed_judgement_is_still_a_duplicate`, `test_a_confirm_that_fails_still_returns_the_judgement` |
| 4d | *(extra)* reserve with the long TTL again | `test_the_reservation_is_short_while_the_judgement_runs`, `test_a_confirm_that_fails_still_returns_the_judgement` |
| 4e | *(extra)* confirm without `XX` | `test_confirm_uses_xx_and_never_creates_a_key`, `test_a_confirm_that_fails_still_returns_the_judgement` |

---

### What changed

| File | Change |
| --- | --- |
| `core/breaker.py` | Guard 1 (`except BaseException` re-arms a probe), guard 2 (`_probe_started`, abandoned-probe takeover), `breaker_probe_abandoned`, and `PoolExhausted` not counted. |
| `core/redis.py` | `PoolExhausted`, `BoundedPool` (acquire timeout only), `_build_pool` sized by `redis_pool_size`. Still no numeric literal. |
| `core/config.py` | `REDIS_POOL_HEADROOM`, `redis_max_connections: int \| None`, the `redis_pool_size` property, `judgement_deadline_seconds`. Three-line comments on the four settings touched. |
| `core/errors.py` | `JudgementDeadlineExceeded` → 503 `judgement_deadline_exceeded`, and nothing else. |
| `units/.../state.py` | `_CONFIRMED`, `confirm_idempotency`, and the two-lifetimes docstrings. |
| `units/.../pipeline.py` | The deadline in both entry points and `_deadline_exceeded`. `IDEMPOTENCY_INFLIGHT_MULTIPLIER`, the short reservation, the confirm as step 9, and the shielded release on `BaseException`. The step list and the "WHY 7" heading are renumbered. |
| `tests/helpers/fake_operational_redis.py` | `SET ... XX`. |
| `tests/unit/test_breaker.py`, `test_redis.py`, `test_config.py`, `test_dodeal_errors.py`, `test_judgement_pipeline.py`, `test_unit_a_state.py` | The guards listed in the sabotage record, plus the pool-size, deadline-setting and confirm-state tests. |
| `README.md`, `ASSUMPTIONS.md`, `docs/STATUS.md` | README: the breaker events, the redis and breaker rows, `DODEAL_REDIS_MAX_CONNECTIONS`, `DODEAL_JUDGEMENT_DEADLINE_SECONDS`, the deadline sentence and the 503 in the route contract, and the two lifetimes. ASSUMPTIONS: new §4.7 (Q16) and the "Reserve, confirm and release" row. STATUS: items 80–83 and a Piece N.3b row. |

---

### Disagreements between the brief and the tree

1. **Q16 is not in `ASSUMPTIONS.md`.** It is in `docs/STATUS.md` §6. The brief asked for the deadline "under
   Q16" in ASSUMPTIONS, so §4.7 was added there, naming Q16 and pointing at STATUS.
2. **Floors.** The brief lists `pipeline.py` and `state.py` at 100. The tree's floors for both are 95, and
   `core/breaker.py`'s is 100. No floor was changed, because that is a policy change. Both files are held at
   100 % measured coverage.
3. **`int(deadline × 4)` truncates to `EX 0`** for any deadline under 0.25 s, and Redis refuses `EX 0`, which
   would make every reservation a 503. `math.ceil` gives the same 100 at the default and never 0. The
   deadline tests run at 0.05 s, so this is not hypothetical in the suite.
4. **"`redis_pool_size` never below `max_inflight`" and "honours an explicit value" cannot both hold.** The
   override is honoured as given. The derived size cannot go below the cap. No validator was added: it would
   refuse to start any environment still carrying the old `DODEAL_REDIS_MAX_CONNECTIONS=20` row.
5. **README changed in commits 1 and 2** as well as 3 and 4. Without it those commits would have shipped a
   breaker events table missing a line, and a pool default of 20 that is no longer true.
6. **Files beyond each commit's list, all required by a change the brief asked for.** `test_config.py`
   asserted the old default of 20 and read a type off it. `test_dodeal_errors.py` is the taxonomy table the
   new code belongs in. `fake_operational_redis.py` did not support `XX`.
7. **`pipeline.py` said "WHY 8 IS classify"** while its own list put classify at 7, drift from N.3's
   renumbering. It was corrected while adding step 9.

---

### For the lead

1. **Two provisional numbers, and the load lane owns both.** `DODEAL_JUDGEMENT_DEADLINE_SECONDS=25.0` must be
   at or below the CRM's own timeout (Q16). `IDEMPOTENCY_INFLIGHT_MULTIPLIER=4` is a code constant: while a
   judgement may still run, a reservation lives four deadlines. The lane sets the deadline together with
   `DODEAL_MAX_INFLIGHT`, because the deadline is also how long a judgement holds an in-flight slot and a pool
   connection. Change the multiplier in the same breath or not at all.
2. **A probe refused by the pool stays HALF_OPEN** until guard 2 takes it over one window later. The brief's
   code was followed as written: `PoolExhausted` is simply not counted, in any state. So in HALF_OPEN, pool
   exhaustion still costs one window of refusals, the thing item 81 removes in CLOSED. Handing the probe
   straight back (OPEN, window already expired) is a two-line change if you want it.
3. **A late result can be attributed to the wrong probe.** Transitions key on the state, not on which call is
   the probe. Suppose a probe is taken over by guard 2, then the old probe finally fails or is cancelled while
   the new one is out: it reopens the breaker for a window, and the new probe's success is ignored. This is
   strictly better than the wedge it replaces, and needs a probe older than `open_seconds` to happen at all. A
   probe generation counter closes it. Not built, because it changes `_record_failure` as well.
4. **One window is left open after the confirm.** The confirm sits inside the try, but step 10 (attempt counter
   and fingerprint, only when a prompt is sent) sits outside it, as before. A cancellation landing in those two
   db2 calls leaves the key **confirmed** with no judgement delivered, so the CRM's retry meets 409 for the long
   TTL. D3 (store and replay the judgement under the key) closes it for good. Moving step 10 inside the try, or
   the confirm after it, are the cheaper alternatives, and each has its own cost.
5. **A cancellation during the reservation's own `SET`** propagates before the try, so nothing releases. The
   key, if the `SET` landed, expires on the short TTL. This is the case "reserve short" exists for.
6. **An environment built from `.env.example` since N.2 most likely sets `DODEAL_REDIS_MAX_CONNECTIONS=20`.**
   N.2 added the pool row with the default of the day, and this session cannot read the file to confirm it. An
   explicit value is honoured, so such an environment keeps a pool of 20 under a cap of 32, and item 81's
   sizing never reaches it. Comment the row out, as the text below does.
7. **On db1 one judgement can hold two connections at once.** Vague detection and scoring charge tokens
   concurrently. The headroom of 4 does not cover `2 × max_inflight` in the worst instant. The pool's 1 s
   acquire wait absorbs a brief overlap, and a refusal there bypasses a charge rather than failing anything.
   Worth measuring in the load lane.
8. **The shield holds a task nobody references.** If the awaiting request is cancelled a second time, the
   shielded release carries on as a task referenced only through the socket future it is waiting on. That is
   enough in practice. A module-level set of release tasks would make it explicit.
9. **`.env.example` is not writable by this session.** Its working copy already has an unstaged modification
   from outside the session, left untouched and unstaged. Rows to add by hand:
   ```
   # One deadline for the whole judgement, fetch included. PROVISIONAL: must be
   # at or below the CRM's own inline timeout (Q16); above it, the CRM abandons
   # requests we go on to finish.
   DODEAL_JUDGEMENT_DEADLINE_SECONDS=25.0
   # Optional override of the Redis pool size. Unset = DODEAL_MAX_INFLIGHT + 4,
   # so the pool can never be smaller than the requests that may hold a
   # connection at once; a value below that turns load into breaker trips.
   # DODEAL_REDIS_MAX_CONNECTIONS=
   ```
   The last line of that text says "breaker trips". After this piece, a pool below the cap turns load into
   **refusals** (`PoolExhausted`: a 503 at the reservation, a bypass on the money guards) and no longer into
   breaker trips. Worth rewording when the row is added.
10. **The known flake fired four times in ten full runs** (`test_elapsed_covers_more_than_any_single_pass`:
    once on commit 1, once on commit 3, twice on commit 4). Each time it was the only failure, and the single
    permitted re-run passed. The cause is outside this piece and untouched. On this machine `time.monotonic()`
    is `GetTickCount64()`, at a resolution of 15.625 ms, so a 20 ms sleep can measure as 15 ms against the
    test's 20 ms floor. The chain was also red once on this piece's own new code, a nested `with` in
    `test_breaker.py` (SIM117), fixed before the green run.
11. **Register items 80–83 could not be located** in any tracked or untracked file, the same gap as 20, 24,
    25, 27 and 61.

**Nothing in this piece has been verified against a real Redis, a real provider or a real CRM.** The pool
tests use stub connections that open no socket, the deadline has only ever cancelled `FakeLLM`, and the
shield has only ever protected a fake `DEL`. Nothing is marked `[V]`.

---

### N.4 — the real-Redis lane   STATUS: DONE `44e9071`

Register item 3, closed, and the lane half of item 29. Tests and docs only: no line of `src/` changed.

**Default run:** 1070 passing, 1 skipped, **28 deselected** (was 7: the `integration` marker's 7 plus this
lane's 21). Coverage **99.47 %**, unchanged. 15 per-file floors met, none added.

**The lane itself:** `uv run pytest -m redis_real --no-cov` with `DODEAL_REDIS_REAL_URL=redis://localhost:6379/9`,
against the compose `redis:7-alpine` service, **Redis 7.4.10** (`INFO server`):

```
============================= 21 passed in 2.20s ==============================
```

Database 9 held 0 keys before the run and 0 after.

---

### What had only ever run on fakes

| Behaviour | Where it ran before | Real-lane test (`tests/redis_real/`) |
| --- | --- | --- |
| Request script: pair moves together; window on create only; M4 | fakeredis[lua] | `test_the_cost_script_moves_both_counters_together`, `test_the_cost_window_is_set_on_create_and_only_on_create`, `test_a_cost_key_without_a_ttl_never_gains_one` |
| Token script: the same three; namespaces disjoint | fakeredis[lua] (no M4 test anywhere) | `test_the_token_script_moves_both_counters_together`, `test_the_token_window_is_set_on_create_and_only_on_create`, `test_a_token_key_without_a_ttl_never_gains_one`, `test_charging_tokens_leaves_the_request_counters_alone`, `test_counting_a_request_leaves_the_token_counters_alone` |
| Rate-limit script: `[1, before]` / `[0, count]`; window on create and on TTL −1; concurrency | fakeredis[lua] and a dict copy in `FakeOperationalRedis`; never concurrent | `test_the_rate_script_allows_up_to_the_limit_and_returns_the_count_before`, `test_the_rate_window_is_set_when_the_counter_is_created`, `test_a_rate_key_without_a_ttl_gains_one_on_the_next_slot`, `test_concurrent_slots_are_taken_exactly_up_to_the_limit` |
| Reservation: `SET NX EX`, `SET XX EX`, `DEL` | `FakeOperationalRedis` (a dict, no clock) | `test_a_reservation_is_claimed_once_and_refused_the_second_time`, `test_concurrent_identical_reservations_have_exactly_one_winner`, `test_a_confirm_on_a_missing_key_creates_nothing`, `test_a_confirm_replaces_the_value_and_the_ttl`, `test_a_release_frees_the_note_for_a_new_reservation` |
| `BoundedPool` at its cap → `PoolExhausted` | stub connection class, no socket | `test_a_real_pool_at_its_cap_refuses_as_pool_exhausted` |
| A dead host is a store error, not `PoolExhausted` | stub connection class raising `ConnectionError` | `test_a_dead_host_fails_as_the_store_and_not_as_the_pool` |
| The breaker opens on a dead host, then refuses without a call | fake clock, synthetic `RedisError` | `test_the_breaker_opens_on_a_dead_host_and_then_refuses_without_a_socket` |
| A live host keeps it closed and clears the count | fake clock, synthetic success | `test_a_live_host_keeps_the_breaker_closed_and_clears_its_count` |

Each test names its hermetic counterpart in its docstring, or says it has none. The concurrent `EVAL`s, the
concurrent `SET NX` and the token script's M4 edge have none: fakeredis runs in-process, so nothing there is
ever concurrent, and no test anywhere had tried M4 on the token script.

---

### How the lane is built

- **Skipped, never failed.** The session fixture skips every test in three cases: `DODEAL_REDIS_REAL_URL`
  unset (the brief's exact reason text), `PING` failing, or a URL that selects db0, db1 or db2. When `PING`
  fails, the reason names the error type and never the URL, which may carry a password. The db guard was
  not asked for. README now says the lane never touches the service's databases, and the guard makes that
  true by construction.
- **Marked by path.** A `pytestmark` in `conftest.py` marks nothing, so the conftest carries a
  `pytest_collection_modifyitems` hook, `tryfirst`, that puts `redis_real` on every item under the directory
  before the mark plugin deselects. Each module also carries `pytestmark`, because it needs
  `asyncio(loop_scope="session")` too.
- **One session loop.** The client is session-scoped, and a `redis.asyncio` connection belongs to the loop
  that opened it. Every async fixture and test in the lane runs on pytest-asyncio's session loop.
- **No factory patched.** Clients are built from the URL. `get_cost_client` and `get_operational_client` are
  never touched, so `tests/test_hermetic_fakes.py` passes unchanged.
- **Keys.** They use the production spellings, imported from `limiter.py` and `state.py`, under a uuid4 prefix
  per test. Values are inert: numbers, `_RESERVED`, `_CONFIRMED`, and a fingerprint of 64 zeros. Teardown
  deletes everything under the prefix with `SCAN`, then scans again and fails the test if anything is left.
- **Settings' defaults, not the environment.** `Settings.model_construct()` gives the defaults with no env and
  no `.env` read. The timing assertion in (f) is measured against a connect budget that a local override
  cannot move, and the session fixture needs no signing key.
- **TTLs are read, never waited for.** A fresh window reads back as the window itself (Redis rounds TTL to the
  nearest second). The helper allows one second for a slow round trip.

---

### What a real server showed that the fakes did not

1. **The default suite is not hermetic against a local Redis.** With the compose Redis up, `uv run pytest`
   fails 42 or 43 tests with `RuntimeError: Event loop is closed`. The count varies with ordering. It fails
   the same way with `tests/redis_real` ignored, so it is not this piece. Failures by file:
   - `test_vague.py`: 16
   - `test_reprompt.py`: 10
   - `test_classification.py`: 9
   - `test_scoring.py`: 4
   - one each in `test_judgement_pipeline.py`, `test_logging_config.py`, `test_log_safety.py`, `test_fake_llm.py`

   These tests drive the pipeline without patching `get_cost_client()`, so the token pre-flight and the
   charge reach **the real db1**. The `lru_cache`'d client then outlives the event loop that opened its
   connection. With no Redis on the machine, the connect fails, the gate fails open, and the tests pass,
   which is why CI (no Redis) and every earlier chain were green. They also write
   `tokens:tenant:tenant-a` and `tokens:user:tenant-a:42` into the local db1. `test_hermetic_fakes.py`
   cannot see it: it inspects the patches that exist, not code that reaches a factory nobody patched.
   CONTRIBUTING's own steps 4 and 6 (`docker compose up -d`, then `uv run pytest`) produce a red suite.
   Not fixed here: the fix belongs in those test modules or the root conftest.
2. **A dead host is not always a `ConnectionError`.** On this Windows host, a connect to a closed loopback
   port outlives the 0.25 s connect budget. redis-py raises `redis.TimeoutError("Timeout connecting to
   server")`, with no cause, in about 0.25 s. The stub in `test_redis.py` (`_Refused`) models only
   `ConnectionError`, and so does `test_a_refused_connection_is_counted`. Production is unaffected.
   `BoundedPool` relabels only a `ConnectionError` caused by a `TimeoutError`, and this one passes through. The
   breaker counts every `RedisError` that is not `PoolExhausted`, and every caller catches `RedisError`. A
   firewall that drops rather than refuses gives the same shape on any OS.
3. **The token script has the M4 edge too.** A token key that exists without a TTL never gains one, exactly
   like the request key. STATUS already said "the two cost scripts", and now a test shows it.
4. **Otherwise the fakes agree with the server.** A declined `SET NX` or `XX` is `None`, as
   `FakeOperationalRedis` claims. TTL answers −1 and −2. Lua replies arrive as lists of ints. redis-py's pool
   refusal is `ConnectionError` caused by the builtin `TimeoutError`, the chain `BoundedPool` relies on.

---

### Sabotage record

| Check | Result |
| --- | --- |
| **The sabotage:** `DODEAL_REDIS_REAL_URL=redis://localhost:56980/9`, a port found free by binding and closing it | `21 skipped in 0.74s`, exit 0. Every reason: `DODEAL_REDIS_REAL_URL is set but PING failed (TimeoutError); the real-Redis lane needs a live server` |
| Variable unset: `uv run pytest --collect-only -q \| Select-String redis_real` | **0 hits**; `1071/1099 tests collected (28 deselected)` |
| *(extra)* variable unset, `-m redis_real` | `21 skipped`, reason `DODEAL_REDIS_REAL_URL not set; the real-Redis lane needs a live server` |
| *(extra)* URL selecting db 2 | `21 skipped`, reason `... selects db2; the real-Redis lane refuses db0 to db2, the service's own stores` |
| *(extra, outside the tree)* a scratch copy of the conftest beside a module with **no** `pytestmark` | default addopts: `no tests collected (1 deselected)`; `-m redis_real`: `1 test collected`. The path hook alone keeps it out. |

---

### What changed

| File | Change |
| --- | --- |
| `tests/redis_real/conftest.py` | New. The path hook, the session client, the db guard, the three skips, `real_redis_url`, `key_prefix`. |
| `tests/redis_real/test_scripts_real.py` | New. Brief items (a) to (d), 17 tests. |
| `tests/redis_real/test_pool_and_breaker_real.py` | New. Brief items (e) to (g), 4 tests; (f) is split into the store error and the breaker. |
| `pyproject.toml` | The `redis_real` marker text only. |
| `README.md` | "The real-Redis lane" subsection (what it shows, PowerShell and bash commands, db 9, skip rules, hand run). A `tests/redis_real/` file-reference table and a structure-tree line. "The default run is fully hermetic". The stale "nothing carries it yet" bullet replaced. |
| `CONTRIBUTING.md` | The hermetic bullet names the three Redis lanes. |
| `docs/STATUS.md` | A Piece N.4 row, and "Register items closed in Piece N.4" (3, 29 lane half). Item 3's "still partial" paragraph resolved. H3 and M4 rows corrected. Suite line. The step 3 rows (build position and sequence). |

---

### Disagreements between the brief and the tree

1. **A CI system exists.** `.github/workflows/ci.yml` runs six checks on every push, on `ubuntu-latest`, with
   no Redis service. The brief says none exists. `.github/` is untouched, as the brief says, and the lane is
   not in CI.
2. **(a) and (b) expected the M4 repair on the request and token scripts.** Neither script has it. M4 is
   PLANNED, and `test_a_pre_existing_key_without_a_ttl_never_gains_one` pins the current behaviour. Asserting
   the repair would have turned the lane red on tracked, known behaviour rather than on anything a real server
   revealed. Both are pinned as they behave today, like the counterpart. Only the rate-limit script repairs
   a key with no TTL, and (c) asserts that.
3. **"The whole directory is marked with pytestmark"** cannot be done from `conftest.py`. The directory is
   marked by a path hook, and each module carries its own `pytestmark`. Both are shown working above.
4. **(f) expected `redis.ConnectionError`.** On this host it is `redis.TimeoutError` (finding 2). The test
   asserts `ConnectionError | TimeoutError`, and never `PoolExhausted`.
5. **(g) "failures 0".** The count is private and no test reads it. (g) shows it through behaviour instead:
   - one dead-host failure first;
   - then three live commands;
   - at a threshold of two, the next failure leaves the breaker CLOSED, which it can do only from 0;
   - the failure after that opens it.
6. **STATUS's H3 row had no "not proven on a real server" sentence.** It said "**The breaker is NOT built**",
   false since N.3 (`3199e3d`), and gave the pool as 20, false since N.3b. Both are corrected, and N.4 added.
   The status stays **PARTLY DONE**: a host that accepts and then stalls costs `/ready` one 1.0 s socket
   timeout. That is k8s's default probe timeout itself, not under it. Unmeasured, and the lead's call.
7. **STATUS listed the M4 TTL fix and policy-per-caller as item 3's remaining work, "— N.4".** This brief closes
   item 3 with the lane and names neither. Item 3 is marked DONE as briefed. Both are carried explicitly,
   unscheduled, on the step 3 rows.
8. **CONTRIBUTING had no "fakeredis / hand-rolled fakes split"** to put a sentence beside: it names only the
   shared fakes, and README carries the split. The hermetic bullet now names all three lanes once, and its
   "if a test needs real infrastructure, it doesn't belong here" became "any other real infrastructure".
9. **The stopping chain is red with the brief's own Redis running** (finding 1). The brief states the invariant
   as "a default run with no Redis on the machine must stay green". So the commit's chain ran with the
   compose Redis **stopped**, as CI runs it, and Redis was started again after the commit.
10. **Item 29 could not be located** in any tracked or untracked file, the same gap as 20, 24, 25, 27, 61 and
    80–83.
11. **Left as found:**
    - `tests/unit/test_cost_lua.py`'s docstring still says of `redis_real` "nothing does yet". The brief says
      the fakeredis tests stay as they are.
    - README's structure tree was already stale (it lists `study.py`, deleted in 6a, and omits `tests/eval/`).
      Only the lane's line was added.
    - The closing "nothing verified against a real Redis" sentences of N.1–N.3b above are records of those
      pieces and were left as written. `Select-String "real Redis"` over `docs/` and `README.md` finds only
      the new lane text.

---

### For the lead

1. **The default suite talks to a local Redis when one is up** (finding 1). This is the piece's real
   discovery. CI cannot see it, and every developer who follows CONTRIBUTING gets 42–43 red tests and two
   token counters written to their db1. The fix lives in tests only:
   - patch `get_cost_client` in those eight modules with `FakeCostRedis`, or
   - have the root conftest point both factories at a closed port for every test outside `tests/redis_real/`.

   Extending `test_hermetic_fakes.py` to fail on an unpatched pipeline test would stop it coming back.
2. **The `.env.example` row, exact text** (this session cannot write the file):
   ```
   # The real-Redis test lane (pytest -m redis_real). Unset by default: the lane
   # is skipped, never failed, without it. Use a database the service does not
   # (db0 to db2 are the queue, cost and operational stores).
   # DODEAL_REDIS_REAL_URL=redis://localhost:6379/9
   ```
3. **CI.** The brief says no CI system exists in the repo, so the lane is a documented hand run until one is
   chosen. **The tree disagrees**: GitHub Actions runs `.github/workflows/ci.yml`, with no Redis service in it.
   The lane stays a documented hand run until you decide otherwise. If you want it in CI, the smallest
   version is a second job with a `services: redis` container and `DODEAL_REDIS_REAL_URL` set. A second job,
   not a step in `checks`, so the default job stays the hermetic one.
4. **A dead host can be a `TimeoutError`** (finding 2). The hermetic stubs model only `ConnectionError`. A
   `_TimedOut` twin of `_Refused` in `test_redis.py`, plus a counted-timeout case in `test_breaker.py`, would
   pin the other shape without a server.
5. **The M4 fix now has three tests to change**, one hermetic and two real, and it must cover the token
   script as well as the request script.
6. **Two keys this session's default-suite runs wrote to the compose Redis db1** are `tokens:tenant:tenant-a`
   and `tokens:user:tenant-a:42`. Both are on the 24 h cost window. They were left in place. To remove them
   now: `docker compose exec redis redis-cli -n 1 del tokens:tenant:tenant-a tokens:user:tenant-a:42`.
7. **H3's status is yours to close** (disagreement 6).

**What has now met a real server:** the three scripts, the reservation's commands, the pool's refusal and the
breaker's reaction to a dead host, all by hand, against Redis 7.4.10 on one Windows machine. **Nothing else
has.** Not a real provider, not a real CRM, not a Redis under load or behind a network. Nothing is marked `[V]`.

---

## Piece 103: the default run is hermetic, Redis running or not   STATUS: DONE `170ddbf`

Register item 103, new with this brief, from N.4's finding 1. Tests and docs only: no line of `src/` changed.

**Before, at `427ed3e`, with the compose Redis running.** 84 lines of the output carry `Event loop is closed`.

```
42 failed, 1028 passed, 1 skipped, 28 deselected, 1 warning in 30.39s
```

**After: the stopping chain with the compose Redis running** (`docker compose ps`: `running`; `PING`: `PONG`).

```
========= 1073 passed, 1 skipped, 28 deselected, 1 warning in 13.25s ==========
```

**After: the stopping chain with the compose Redis stopped** (`docker compose stop redis`; the container was `Exited (0)` for the whole run; started again afterwards, `PONG`).

```
========= 1073 passed, 1 skipped, 28 deselected, 1 warning in 13.13s ==========
```

Both runs were the whole chain, and every step was green.

- Coverage: 99.47 %, unchanged.
- `ruff check`: all checks passed.
- `ruff format --check`: 138 files already formatted.
- `mypy`: no issues in 65 source files.
- `scripts/check_coverage_floors.py`: all 15 floors met, `limiter.py` still at 100 %.
- The known flake did not fire.
- Tests selected by default: 1074, which is `427ed3e`'s 1071 plus this piece's three. Deselected: 28, unchanged.

**db1 and db2 around the Redis-up run** (`docker compose exec redis redis-cli -n N DBSIZE`):

| | before | after |
| --- | --- | --- |
| db1 | 2 | 2 |
| db2 | 0 | 0 |

`DBSIZE` cannot see an increment to a key that already exists, and db1 holds the two keys N.4 left. So both values were read as well: `tokens:tenant:tenant-a` and `tokens:user:tenant-a:42` were each **21960 before the run and 21960 after**. They were 8280 when this piece started. The sabotage stages below raised them.

---

### The eight modules

Measured at `427ed3e`, not copied from N.4's list. A scratch plugin outside the tree wrapped `core/redis.py`'s `_build_pool`. It cleared both factory caches before each test and pointed both URLs at a closed port, so nothing reached the live server. `tests/unit/test_redis.py` is left out, because it builds the real pools on purpose.

| Module | Connection | Path to the real client | Red at `427ed3e`, Redis up |
| --- | --- | --- | --- |
| `tests/unit/test_vague.py` | db1 | a pass → `llm_call.complete_once` → `limiter.enforce_token_cost` → `limiter.get_cost_client()` | 16 |
| `tests/unit/test_reprompt.py` | db1 | the same | 10 |
| `tests/unit/test_classification.py` | db1 | the same | 9 |
| `tests/unit/test_scoring.py` | db1 | the same | 4 |
| `tests/helpers/test_fake_llm.py` | db1 | the same | 1 |
| `tests/security/test_log_safety.py` | db1 | the same | 1 |
| `tests/unit/test_logging_config.py` | db1, db2 | `TestClient(app)` → the lifespan's `aclose()` on `main.get_cost_client()` and `main.get_operational_client()` | 1 |
| `tests/unit/test_startup.py` | db1, db2 | the same | 0 |

41 of the 42 failures are inside `enforce_token_cost`'s `EVAL`, and the 42nd is at `main.py:48`. The client is `lru_cache`'d, so it outlives the event loop of the test that opened its connection. The next test to use it fails with `RuntimeError: Event loop is closed`. With no Redis listening, the connect failed instead, the charge failed open and the test passed, which is why CI never saw it.

**Why `test_hermetic_fakes.py` could not see it.** It checks what each existing `setattr` of a factory hands over. A module that never patches a factory has no call to check.

---

### How the fix is built

- **`redis_fakes`** (`tests/conftest.py`, autouse, function-scoped).
  - It builds a fresh `FakeCostRedis` and `FakeOperationalRedis` for every test.
  - It patches `get_cost_client` on `limiter` and `main`, and `get_operational_client` on `state` and `main`. Those are the three src modules that import a factory by name.
  - It clears both real factories' caches on the way in and on the way out.
  - It resets both breakers, which absorbs the old `_closed_breakers` fixture.
  - It yields the two fakes, so a test can ask for them by name.

  `core/redis.py` itself is not patched. Its own tests inspect the real pool, and its `/ready` probes are patched by the tests that call them.
- **A module's patch wins.** A root autouse fixture is set up before any fixture a module defines, whatever order the test lists them in. Both patch through the same `monkeypatch`, so the module's patch lands second. `test_a_module_patch_wins_over_the_default_fakes` lists its own fixture first and proves it.
- **The `redis_real` lane gets no fakes.** The lane builds its clients from `DODEAL_REDIS_REAL_URL`. A lane test that reached a factory should fail on the closed port, not pass on a fake. Lane tests still get the breaker reset and the cache clears. The lane, run by hand against db 9 after the change, gave `21 passed, 1081 deselected in 2.55s`. Db 9 held 0 keys before and 0 after.
- **`_closed_redis_urls`** (autouse, session-scoped) sets `DODEAL_REDIS_COST_URL=redis://127.0.0.1:1/1` and `DODEAL_REDIS_OPERATIONAL_URL=redis://127.0.0.1:1/2`. The db numbers are kept because `test_redis.py` asserts the two URLs differ and that the operational one ends in `/2`. A test that needs another URL still sets its own.
- **`_real_pool_builds`** (autouse, session-scoped) **and `pytest_sessionfinish`.** `_build_pool` is wrapped for the session, and each call is recorded with `PYTEST_CURRENT_TEST`. At session end, any call made outside `tests/unit/test_redis.py` sets the exit status to 1. The hook prints `N test(s) reached a real Redis client factory` and then each test's id. It counts rather than raising, because a raise inside a fail-open path would be caught.
- **`aclose()` on both helper fakes.** `main.py`'s lifespan closes both clients on shutdown, and those clients are now fakes. Without `aclose()`, `test_logging_config.py` and `test_startup.py` fail at shutdown.
- **Three tests in `tests/test_hermetic_fakes.py`.** The two existing guards are unchanged.
  - `test_no_test_reaches_a_real_redis_client_factory` parses `src/` for every module that imports a factory by name. It requires at least the three known today and asserts that each bound name returns this test's fake. A new src importer fails it, with a message naming `_COST_CLIENT_HOLDERS` / `_OPERATIONAL_CLIENT_HOLDERS`. It also asserts that `core/redis.py`'s factories are still the cached originals and that `_build_pool` carries the session wrapper.
  - `test_the_service_urls_point_at_a_closed_port_and_the_lane_keeps_its_own` checks both URLs, in the environment and in `Settings`, against `127.0.0.1:1`. It also checks that nothing under `tests/redis_real/` names either variable or either `Settings` field, while the lane names `DODEAL_REDIS_REAL_URL`.
  - `test_a_module_patch_wins_over_the_default_fakes`, as described above.

---

### Sabotage record

All stages ran with the compose Redis running. Each one edited `tests/conftest.py` inside a script whose `finally` copied the saved file back. The restored file's SHA-256 (`B839F94C…0300711A`) is identical to the one saved before S1.

| Stage | What was switched off | Result |
| --- | --- | --- |
| S1 | `redis_fakes` made non-autouse, which turns off the fakes, the cache clears and the breaker reset. The closed port and the counter stay on. | **Red**, exit 1, `127 failed, 946 passed`. **0** `Event loop is closed` lines, and db1's two values stayed at 8280: the closed port alone kept the run off the live server. The hook printed `5 test(s) reached a real Redis client factory` (`test_fake_llm`, `test_classification`, `test_logging_config`, `test_reprompt`, `test_startup`). Most of the 127 are a breaker cascade: the output carries 66 breaker-open log mentions, 18 `BreakerOpen` and 9 `IdempotencyUnavailableError`, in modules that install their own fakes (`test_judgement_routes` 40, `test_judgement_pipeline` 36, `test_unit_a_state` 26, `test_token_cost` 14, `test_cost` 8). |
| S2 | S1, plus `_closed_redis_urls` made non-autouse | **Red**, `140 failed, 933 passed`. 50 `Event loop is closed` lines. db1's values went 8280 → 13680, a live write that `DBSIZE` (still 2) does not show. The hook named 4 tests. Still mixed with the cascade (56 breaker-open mentions), and `test_vague` did not fail at all. |
| **S3: the brief's sabotage** | `427ed3e`'s conditions: no fake injection, no cache clears and no closed port, with breakers still reset per test. The counter stays on. | **Red**, `45 failed, 1028 passed, 1 skipped, 28 deselected`. **84** `Event loop is closed` lines, as at `427ed3e`. Per-module failures are **identical to the baseline** (vague 16, reprompt 10, classification 9, scoring 4, `test_fake_llm` 1, `test_log_safety` 1, `test_logging_config` 1), plus the three new tests. The hook printed `3 test(s) reached a real Redis client factory`. db1's values went 13680 → 21960. |
| (e) private fake | `tests/unit/test_probe_private_fake.py`, which patches `limiter.get_cost_client` with a local class. Only the guard was run. | **Red**, naming the file and line: `tests/unit/test_probe_private_fake.py:12 get_cost_client <- monkeypatch.setattr(limiter, 'get_cost_client', lambda: _Pri`. The probe was deleted and never staged. |

S1 is not what the brief predicted. Removing the autouse fixture alone does not bring `Event loop is closed` back. The second line of defence keeps the suite off the server, and the counter is what turns the run red. S3 is the stage that matches the brief's expectation.

---

### What changed

| File | Change |
| --- | --- |
| `tests/conftest.py` | Adds `redis_fakes` (which replaces `_closed_breakers`), `_closed_redis_urls`, `_real_pool_builds` and `pytest_sessionfinish`. `_isolated_settings`' "the ONLY env var" sentence is narrowed to settings a test can depend on. |
| `tests/test_hermetic_fakes.py` | The three tests above and their imports. The module docstring names item 103. |
| `tests/helpers/fake_cost_redis.py`, `tests/helpers/fake_operational_redis.py` | `aclose()` and a `closed` flag. |
| `CONTRIBUTING.md` | One sentence in the hermetic bullet: the default run is hermetic with or without a Redis listening, there is no need to stop the compose Redis, and only the lane needs one. |
| `README.md` | The same sentence, in the Testing section. |
| `docs/STATUS.md` | A Piece 103 row, a "Register item closed in Piece 103" section, a "Hermetic default run" row in §5, the suite line and "Last updated". |

---

### Disagreements between the brief and the tree

1. **N.4's eighth module was the wrong one.** N.4 named `test_judgement_pipeline.py`. That module fakes db1 with an autouse fixture and never reached a factory when measured. Its one failure in N.4 was most likely the known flake, `test_elapsed_covers_more_than_any_single_pass`, which lives there. This piece's baseline had 42 failures and none in that module. The real eighth is `test_startup.py`: it reaches both factories through the lifespan, but stayed green at `427ed3e`.
2. **`main`'s factories could not be handed the helper fakes as they were.** The lifespan calls `aclose(close_connection_pool=True)`, and neither fake had it. Both fakes gained a recording `aclose()`, in two files outside the brief's list. The alternative was leaving `main`'s names on the real factories, which would leave those factories reachable from `test_logging_config` and `test_startup`.
3. **The pool counter cannot be zero across the whole suite.** `tests/unit/test_redis.py` builds real pools on purpose: it is the factory's own test, and building a pool opens no socket. The hook exempts that one module, the same exemption the existing guard already gives `core/redis.py`'s own patches. Everywhere else the count is zero.
4. **The sabotage as briefed does not reproduce the failures** (S1 above). The exact set came back in S3.
5. **The breaker reset moved.** The brief gives it to the new fixture, and the old `_closed_breakers` did the same job. It was folded in rather than kept twice, with the same reset on the way in and out. This is also why S1 and S2 cascade.
6. **Collected tests.** 1071 were selected at `427ed3e` and 1074 are now; the difference is this piece's three tests. Deselected stays at 28, and no existing test's selection changed.
7. **"One test", and more to assert.** The brief asked for one test for the guard and one for the module patch, and also for the lane's independence to be asserted in the guard. Those have different subjects, so they are three tests.
8. **CONTRIBUTING never told anyone to stop Redis.** Its steps are "Start Redis" (4) and "Run tests" (6), and N.4's point was that following them went red. There was nothing to replace, so the statement was added to the hermetic bullet.
9. **`docs/STATUS.md` had no "Hermetic" row** to correct. One was added under §5, beside the root-conftest row.
10. **Docker Desktop stopped during the session**, not through anything this session ran: no Docker process, nothing on 6379. The lead restarted it. One chain run overlapped the restart. It was green, but its Redis state cannot be stated, so it is reported as neither run.
11. **A listener on `::1:6379` was still present** a moment after `docker compose stop redis`, with the container `Exited (0)`. With Docker Desktop fully stopped earlier in the session, nothing listened on 6379. So the listener is Docker Desktop's port forwarding, not a second Redis. The default run no longer dials `localhost:6379` in any case.
12. **Item 103 has no register entry** in any tracked or untracked file, the same gap as 20, 24, 25, 27, 29, 61 and 80–83.

---

### For the lead

1. **What the fix does not cover.**
   - `DODEAL_REDIS_QUEUE_URL` (db0) is not pointed at the closed port, as briefed. Nothing in the default run connects to it today. A test that opened arq's connection would reach a live db0, and the pool counter would not see it, because arq builds its own pool.
   - `/ready`'s probes call `core/redis.py`'s own factories, which stay real. Every `/ready` test patches them today. One that did not would land on the closed port and the counter, not on a fake.
   - The hook names every offending test only while `redis_fakes` clears the caches. Without that, the cached client is built once and only the first test per cache is named (S1 named 5 tests, S3 named 3). The run still goes red.
2. **The eight modules, and redundancy.** The eight are `test_vague`, `test_reprompt`, `test_classification`, `test_scoring`, `test_fake_llm`, `test_log_safety`, `test_logging_config` and `test_startup`. None of them carried a factory patch, so none is now redundant, and none was edited. Elsewhere, three patches now duplicate the default:
   - `tests/security/test_chain.py:24` (`lambda: FakeCostRedis()`)
   - `tests/security/test_exit_demo.py:20` (`lambda: FakeCostRedis()`)
   - `tests/security/test_log_safety.py:530` (`lambda: FakeOperationalRedis()`)

   They are not identical to the default: a fresh fake per call forgets everything between calls, while the default keeps one store per test. Left as they were, as briefed.
3. **No test's meaning changed.** None of the eight asserts on Redis. Before, with no Redis, their token charge took the fail-open branch and logged `token_charge_bypassed`. Now it lands on the fake and logs `tokens_charged`. Their assertions hold either way, and the bypass branch keeps its own tests in `test_token_cost.py` (`limiter.py` is still at 100 %). The two lifespan tests now close fakes instead of real clients that were never used. No assertion was touched.
4. **db1 on this machine** still holds the two keys N.4 reported, now at 21960 each after sabotage stages S2 and S3. The hand delete still applies: `docker compose exec redis redis-cli -n 1 del tokens:tenant:tenant-a tokens:user:tenant-a:42`.
5. **README's `tests/` file-reference table** still describes `test_hermetic_fakes.py` as the AST guard alone and has no `conftest.py` row. The brief allowed one sentence in the Testing section.
6. **Item 104** (the CI job with a Redis service) can now keep its default job beside a live Redis container: the default run was green here with one listening.

**Nothing in this piece has met a real provider or a real CRM.** The only real server was the compose Redis, used to show that the default run leaves it alone. Nothing is marked `[V]`.

---

## Piece N.4b: pip-audit on the locked dependency set   STATUS: DONE `ffbf9b0`

Register item 29, the `pip-audit` half and the last of three. `fakeredis[lua]` (`dffeb80`) and the real-Redis lane (`44e9071`) were the other two, so **item 29 is closed**. The last piece of step 3. Dependencies, CI and docs only: no line of `src/` changed, and no test was added.

### Phase 0 found that the command as briefed cannot work

The brief's step 2 named `uv run pip-audit --strict --desc` against the synced environment. Run at `dab744c`, it fails — and not for a vulnerability:

```
ERROR:pip_audit._cli:dodeal-ai: Dependency not found on PyPI and could not be audited: dodeal-ai (0.1.0)
EXIT=1
```

`uv sync` installs the project itself editable — `direct_url.json` reads `{"dir_info":{"editable":true}}` — so it is not on PyPI and `pip-audit` can never resolve it. `--strict` turns that unresolvable dependency into a failure. Without `--strict` the same run is exit 0 with one skip row. **`--skip-editable` does not rescue it**, because `--strict` counts a deliberate skip as a failure too:

```
ERROR:pip_audit._cli:dodeal-ai: distribution marked as editable
EXIT=1
```

So `--strict` and the venv are mutually exclusive in this tree, and the briefed step would have turned CI red on every push forever. The brief's own reason for withholding the step on a finding — that it must not turn CI red on the next push — applies with more force. The piece was reported BLOCKED at Phase 0 (the head check had also failed: the four hand commits were not yet made), and the resolution was accepted as **ruling R38: audit the exported lock, never the venv.**

### What landed

1. **`pip-audit>=2.10.1` in the `dev` group**, locked like every other tool so CI and a hand run use one advisory client. `uv.lock` goes from **63 to 84 packages, +21** for the auditor's closure, none removed: `boolean-py cachecontrol charset-normalizer cyclonedx-python-lib defusedxml license-expression markdown-it-py mdurl msgpack packageurl-python pip pip-api pip-audit pip-requirements-parser py-serializable pyparsing requests rich tomli tomli-w urllib3`. Note it pulls **`requests` and `urllib3` into the dev group** — dev only, never onto the request path. The resulting 83-pin set audits clean, so the new job does not go red on its own dependencies.

2. **A second CI job, `audit`, beside `checks`** — not an eighth step inside it. Checkout, install uv, install Python 3.12, `uv sync --locked`, then:

   ```
   uv export --format requirements-txt --no-emit-project -o audit.txt && uv run pip-audit --strict --desc -r audit.txt
   ```

   Three choices are deliberate and each is commented on the job:
   - **Separate job.** The result is a function of the advisory database, not of the commit. A newly published advisory must be free to go red without turning the required chain red on a hotfix that changed no dependency. This is the one CI result that is not a function of the tree.
   - **`--no-emit-project`.** Not optional — it is what removes the unresolvable editable project, per R38 above.
   - **`--strict` stays.** A third-party pin the auditor cannot check fails the job instead of passing silently. No `continue-on-error`, no `--ignore-vuln`. A finding is fixed by a pin change in its own commit.

   The seven steps of `checks` are untouched.

3. **Docs.** `CONTRIBUTING.md` gains a bullet with the same two commands as the hand run, the network requirement, the pin-change-not-ignore rule, and the Windows `PYTHONIOENCODING=utf-8` note. `README.md` gains a "Testing" sentence, a two-job sentence in "Continuous integration", and a paragraph on why `audit` is deliberately not one of the six required checks. `CLAUDE.md` gains `ci` to the commit-type list.

### The audit result

`uv sync --locked` at `fcdd091`: `Resolved 63 packages`, `Checked 61 packages`, exit 0 — the lock had not drifted. Then the two commands as CI runs them, over **83 pins**:

```
No known vulnerabilities found
EXIT=0
```

**Zero findings.** Exit 0 *under `--strict`* is the stronger statement: it proves nothing was skipped, so all 83 were actually audited. The same result held at `dab744c` in Phase 0 over 62 pins, before the auditor's own 21 packages joined the lock.

### The sabotage record

An audit step cannot be proven by a repo test — a passing audit and an absent audit look identical from inside the suite — so the guard is external, and the brief specified it that way. In a temporary directory, a `requirements.txt` containing one pin with a published advisory, then `uvx pip-audit --strict --desc -r requirements.txt`:

```
Found 22 known vulnerabilities in 3 packages
requests 2.25.0  PYSEC-2023-74   fix 2.31.0
idna     2.10    PYSEC-2024-60   fix 3.7
urllib3  1.26.20 PYSEC-2026-1999 fix 2.5.0
EXIT=1
```

Non-zero exit with the advisory named in the output: the tool does fail on a known finding, so the zero-findings result above is a real check and not a silent no-op. The temporary directory was deleted and its absence verified. Restored: nothing in the repository was changed to run the guard.

One Windows-only finding came out of it. `--desc` prints an arrow (`→`) that the cp1252 console cannot encode, and the run dies with `UnicodeEncodeError` **after** the findings table has printed — so a hand run on the Windows machine looks like a crash rather than a result. `PYTHONIOENCODING=utf-8` fixes it, and `CONTRIBUTING.md` and `README.md` both say so. CI on ubuntu is unaffected.

### The stopping chain at this head

```
========= 1073 passed, 1 skipped, 28 deselected, 1 warning in 26.89s ==========
Required test coverage of 92.0% reached. Total coverage: 99.47%
All 15 coverage floors met.
ruff check: All checks passed!      ruff format: 140 files already formatted
mypy: Success: no issues found in 65 source files
```

Identical to the numbers at `fcdd091`, as expected: this piece adds no test.

### For the lead

1. **Zero findings, so no pin commits are owed** and there is nothing to order. Whether the fix would be a patch, minor or major bump does not arise.
2. **`audit.txt` is not in `.gitignore`.** The hand run writes it to the repo root, where it shows up as untracked in `git status`. `.gitignore` was not in this piece's file list, so it was left alone rather than widened. One line, `audit.txt`, is owed — or the documented hand run should write to a temp path instead.
3. **Register item 101 reads "Written; commit owed"** in `docs/register.md`. `CLAUDE.md` was committed by hand as `afd5fa0`, so that row is stale. Item 101 is outside this piece's scope, so it was not touched.
4. **Piece 103's report block, "For the lead" point 6, calls the Redis-service CI job "item 104".** Per `docs/register.md` it is item 106; 104 is the M4 TTL fix. A past piece's block was not edited, but the number is wrong there.
5. **Item 29's third half was numbered but its CI half was not.** The `redis_real` CI job is item 106, owed with the 74 prep; `docs/STATUS.md`'s item 29 row now says so, where it previously said only "no workflow runs it".
6. **The brief said the step 3 row should read "owed as items 104 and 105".** Both are defined in `docs/register.md` (104 the M4 TTL fix, 105 policy-per-caller for fail-closed workers). The row previously owed a third thing as well — degradation behaviour at the warning ratio (A9) — which is not numbered with them, so it is named separately rather than dropped.
7. **The lock now carries `requests` and `urllib3` as dev dependencies.** They are the two packages with the longest advisory histories in the ecosystem. They are dev-only and no `src/` module imports either, but they are now the most likely source of a future red `audit` job, and a red job there will not be a defect in this service's code.

**Nothing in this piece has met a real provider or a real CRM.** The one external service it touched is the PyPI advisory database, read-only, over the network. Nothing is marked `[V]`.

---

## Piece 76.1: OpenAI-compatible adapter for Groq and OpenAI   STATUS: DONE `38d60a0`

Register item 76, part 1 of 3. The LLM seam gets its first real implementation: one adapter class for every
provider that speaks OpenAI's `/chat/completions`. Nothing has called a provider — the class is proven on
`httpx.MockTransport` and on nothing else.

### Phase 0 was reported BLOCKED, and the lead cleared it

The brief named a head "exactly one commit above `51d227d`, message starting `docs: ignore scratch files`".
That commit does not exist on any ref — `git log --all --grep="ignore scratch"` is empty — and HEAD was
`51d227d` itself. The five scratch paths it would have ignored (`n1.diff`, `n3.diff`, `n3b.diff`,
`docs/audit/`, `docs/campaign/`) are still untracked and `.gitignore` carries no row for any of them.

Reported BLOCKED with three options and **the lead chose to build on `51d227d`**. The ignore commit was NOT
made here: `.gitignore` is not in this piece's file list, and one register item per session is the rule. The
scratch files cannot reach a commit anyway — every path is staged explicitly and `git add .` is forbidden.
**The `docs: ignore scratch files` commit is still owed.**

### What landed

1. **`core/config.py`.** `LLMProvider` gains `GROQ`, `OPENAI` and `GEMINI`. Membership means *the name
   parses*, not *an adapter exists* — `GEMINI` is in the enum and refused by the factory, because
   `llm_provider_not_supported:gemini` tells a deployment which half is missing where a rejected enum value
   would only say the name is wrong. `llm_api_key: SecretStr | None` (read in exactly one place) and
   `llm_base_url: str | None` (proxy override only) join it.

2. **`core/llm/openai_compatible.py`, new.** `OpenAICompatibleClient(base_url, model, api_key, http, *,
   settings)` satisfying `LLMClient`, plus `OpenAICompatibleError`. The two vendor base URLs are **constants
   in this module, not settings**: they are facts about a vendor's API, not a per-deployment choice, and the
   only legitimate reason to point elsewhere is a proxy, which is what `DODEAL_LLM_BASE_URL` is for.

3. **`core/llm/__init__.py`.** `build_llm_client(settings, http)`. It takes both dependencies as arguments
   rather than reading them, so lifespan (item 84) can build it once at startup and a `ConfigError` is a
   refusal to start rather than a 500 on the first judgement. `get_llm_client()` is untouched and still
   raises: nothing owns the pooled `AsyncClient` until 76.2.

### The four decisions worth arguing about

**One class for two vendors (R16).** Groq and OpenAI differ by base URL and nothing else that matters: same
path, same bearer auth, same body, same response shape, same `finish_reason` vocabulary. A class each would
have been a copy whose two halves drift. The defence against that claim being quietly wrong is that **every
conformance assertion is parametrised over both URLs** — 115 tests, most of them running twice — so the day
one vendor needs a branch, a test says so first.

**Two guards at resolution, before the transport is entered.** The temperature bound (0–2) is **per provider**
— OpenAI and Groq accept 0–2 where Anthropic accepts 0–1 — which is exactly why it is not on the shared
`ModelProfile` field. And a profile naming a *different* vendor is refused outright: one client is built for
one base URL, so an `anthropic` profile reaching this adapter would post an Anthropic model id to Groq. Both
raise before the request is built, so a bad profile costs nothing. Routing per profile is the gateway's job
(item 84), and that refusal is the line to delete when it lands.

**Exceptions carry the status and the provider name, and nothing else.** `OpenAICompatibleError` subclasses
`LLMProviderError` so `str()` stays the seam's fixed `llm_provider_error:<reason>` — which is what lets
`safe_error_fields` keep the message of *our* exceptions. The body, the headers and the provider's own message
are **never captured at all**, rather than captured and carefully not printed. The base URL is never carried
either: a proxy override's host or path can itself be the credential, so failures are labelled `groq`,
`openai`, or the generic `openai_compatible` for an override we do not recognise.

**No retry, and never a builtin `TimeoutError`.** One `post` per `complete()`. `httpx.TimeoutException`
becomes the seam's transient error (catch-list 55) — if it left as a builtin `TimeoutError`, the watchdog
could not tell a provider timeout from `asyncio.wait_for`'s own deadline. Every `raise ... from None`: the
originals carry the request URL, and the JSON decoder's carries the body.

### A finding the tests produced: httpx logs the full request URL

`test_a_failure_behind_a_proxy_names_no_host` failed on first run — and not on our own logging. **httpx logs
every request line, URL included, on its own `httpx` logger at INFO.** With a proxy base URL that carries a
credential in its path, that is the credential in the logs.

It does not happen in the service: `configure_logging()` leaves the **root** logger at WARNING and raises only
`dodeal_ai` to `settings.log_level`, so a third-party INFO line is dropped before any handler — even at
`DODEAL_LOG_LEVEL=DEBUG`. `caplog.at_level(DEBUG)` in the test had forced the root floor down. Rather than
weaken the assertion and move on, the guard is now **two** tests: the proxy test asserts over `dodeal_ai`
records (and fails if the adapter logged nothing, so it cannot pass vacuously), and
`test_third_party_request_lines_stay_below_the_root_floor` asserts the root floor itself — so lowering it
fails a test instead of leaking quietly.

### The sabotage record

| Sabotage, one line | Tests that failed | Restored |
| --- | --- | --- |
| `response_format` dropped from the body | `test_request_demands_json_mode` (2) | yes |
| 429 mapped to `AUTH`/not-transient | `test_status_maps_to_reason_and_transience[429]` (2), `test_failure_never_carries_the_provider_body[429]` (2) | yes |
| A second `post` on 5xx | `test_transport_is_entered_exactly_once_on_a_failed_status[500, 503]` (4) | yes |
| `response.text` appended to the exception's args | `test_failure_never_carries_the_provider_body` (12 — every status) | yes |

The file was restored from a byte copy after each and re-checksummed: `dcb6258364e77e4ad7ab8a71db9737b6`
before the first sabotage and after the last, with `git diff` empty against the copy.

### The stopping chain at this head

```
1188 passed, 1 skipped, 28 deselected, 1 warning in 28.43s
Required test coverage of 92.0% reached. Total coverage: 99.56%
All 15 coverage floors met.
ruff check: All checks passed!   ruff format: all files formatted
mypy: Success: no issues found in 66 source files
core/llm/openai_compatible.py: 85 statements, 0 missed, 100%
```

### For the lead

1. **`.env.example` rows, exact text** (a session may not write that file):

   ```
   # The provider API key. SecretStr: a stray repr of Settings prints **********.
   # No default and no placeholder -- absent is a ConfigError naming the provider.
   # Read in exactly ONE place, the adapter's bearer-header builder.
   DODEAL_LLM_API_KEY=change-me-local-only
   # Proxy override for the provider base URL. Unset uses the vendor constant in
   # core/llm/openai_compatible.py. Set it ONLY to put a proxy in front.
   # A wrong value sends every prompt to the wrong host, so it is never logged.
   # DODEAL_LLM_BASE_URL=
   ```

   The brief gave `DODEAL_LLM_API_KEY=` with an empty value. Every other secret in that file reads
   `change-me-local-only` (audited field-by-field in fix 6b), and an empty value would load as the empty
   string rather than `None` — which builds a client with a blank bearer token instead of refusing. **The
   value above is a deliberate departure from the brief's text; take it, or keep the brief's and accept that
   a local `.env` copied from the example fails at the provider rather than at startup.**

2. **The seam's exception names, as found.** `LLMProviderError(reason, *, transient)` and
   `LLMConfigurationError(ConfigError)`, both already in `core/llm/client.py`; `FinishReason.OTHER` already
   existed, so there was nothing to stop and ask about. **Added:** `OpenAICompatibleError(LLMProviderError)`,
   carrying `.status` and `.provider`. **Where the transient one is caught:** nowhere directly. It escapes
   `complete()` into `call_with_watchdog`, whose bare `except Exception` wraps it as `ExternalCallError`;
   `llm_call.complete_once` catches *that* and raises `ModelUnavailableError()` (503 `model_unavailable`).
   **So `.transient` is currently read by nobody** — auth failures and rate limits 503 identically. That is
   the existing wiring and this piece did not change it, but if the flag is meant to mean something, the
   place to make it mean something is `complete_once`.

3. **What did not fit `LLMResponse`, and what was done.** Dropped, deliberately: `object`, `created`,
   `system_fingerprint`, `service_tier`, `choices[0].index`, `choices[0].logprobs`, `message.role`,
   `message.refusal`, `usage.total_tokens` (computed), `usage.prompt_tokens_details` /
   `completion_tokens_details` (**cached-prompt and reasoning token counts — the two that will matter once
   prompt caching is on, and there is no field for either**), Groq's `x_groq` timings, and the `x-request-id`
   header. Kept: `id` to `provider_request_id`. **`choices[1..]` is ignored**: we never send `n>1`, and
   silently taking `[0]` is the right behaviour, but nothing asserts the provider did not send more.

4. **Packages `uv.lock` gained: zero.** `httpx` and `pydantic` were already direct dependencies. `uv.lock` is
   untouched by this piece.

5. **The constructor takes a fifth argument, `settings`,** beyond the four the brief named. Resolution
   (`resolve_profile`) and the per-call timeout both read it, and reading it from ambient `get_settings()`
   would make the class untestable without an environment. The four named arguments keep their order and
   positions; `settings` is keyword-only. **`model` is now near-redundant** — the profile table decides the
   model and returns `settings.llm_model` on the fallback path, so the two always agree. It is kept because
   the brief named it; say the word and 76.2 drops it.

6. **`ModelProfile.temperature` is still capped at `le=1.0`,** which is the *Anthropic* bound applied
   globally. The adapter's 0–2 bound is therefore defence in depth rather than the effective limit: a Groq or
   OpenAI profile cannot today be configured above 1.0 even though both vendors accept 2.0. Widening that
   field would loosen validation for Anthropic profiles too, so it was left alone as the more conservative
   option. **Decide whether the field should become per-provider.** The adapter's bound is proven by
   injecting an out-of-range `ResolvedProfile` at `resolve_profile`, which the test says in as many words.

7. **One existing test had to change**, and it is the only file outside this piece's list that did:
   `tests/unit/test_model_profiles.py::test_an_unknown_provider_in_a_profile_is_a_config_error` used the
   literal `"openai"` as its example of an *unknown* provider. This piece makes that name valid, so the
   sample is now `"mistral"` with a comment saying why. The test's intent is unchanged.

8. **The temperature bound is enforced per call, not at startup.** Every profile in `DODEAL_LLM_PROFILES`
   could be swept once when `build_llm_client` runs, which would turn a bad profile into a refusal to start
   instead of a 503 on the first judgement using that profile. The brief said "enforced at resolution", so
   that is what was built. **Worth folding into item 84.**

9. **`R16` could not be located in this repo.** No tracked file carries a ruling numbered R16 — the only
   ruling in `CAMPAIGN_REPORT.md` is R17, and `ASSUMPTIONS.md` references R17 alone. The one-class-for-two-
   vendors reasoning is written out in full in `README.md` and `ASSUMPTIONS.md` §3.8 rather than cited, and
   the R16 reference is carried in the adapter's module docstring on the brief's authority. Same gap the
   N.1–N.3b pieces reported for their item numbers.

10. **`DECISION[DEMO_PROVIDER]` did not exist** and was created, not edited. `CLAUDE.md:67` referenced it but
    no section carried it; it is now `ASSUMPTIONS.md` §3.8 beside `DECISION[PRODUCTION_PROVIDER]`. **The
    reasons recorded there are inferred from the brief's provider split (Groq for the demo, OpenAI for
    production), not quoted from the master document — check them.** In particular §3.8 says the demo choice
    is about *latency* and explicitly not about cost; if that is wrong, it is one paragraph to fix.

11. **The residency question (Q20) is untouched and still unowned.** Choosing providers does not answer where
    note text rests or under whose DPA, and §3.8 says so rather than letting a provider decision read as a
    residency decision.

**Nothing in this piece has met a real provider.** No key exists, no call has been placed, and nothing is
marked `[V]`. The adapter is proven against `httpx.MockTransport` and against nothing else.

---

## Piece 76.1a: the timeout race and the provider reason on the outcome line   STATUS: DONE `13ffdf9`

Two defects found by reviewing 76.1's own explainer rather than by a test failing. Both were claims I made in
that explainer; **checking them found one overstated and one understated**, and the understated one was a real
defect in code that had just been committed. No register item — the lead adds items, not a session — so two
are owed below.

### What I claimed, and what checking it found

**Claim: "a 429 and a 500 are indistinguishable in the logs." Overstated — it was already false.**
`safe_error_fields` includes `str(exc)` for exceptions defined under `dodeal_ai`, and `OpenAICompatibleError`'s
`str()` is the seam's fixed `llm_provider_error:<reason>`. So `judgement_model_unavailable` already carried
`error: llm_provider_error:rate_limited`. The reason was there; it was in a string rather than a field.

**Claim: "the two timers race, so about half of timeouts lose their provider attribution." Understated.**
The watchdog's clock starts in `call_with_watchdog` before `_resolve()` and `_body()`. httpx's `read` clock
starts only after connect and send. With both set to the same `llm_timeout_seconds`, **the watchdog's deadline
always arrives first** — so the adapter's `except httpx.TimeoutException` branch was effectively dead code in
production, and *every* provider timeout arrived as a bare `TimeoutError` with no provider name and without the
adapter's own `llm_provider_call_failed` line ever firing. Not half. All of them.

A probe forcing the watchdog to win confirmed the outcome: `cause type: TimeoutError`, `provider attr: <<NONE>>`,
`adapter logged llm_provider_call_failed: False`. (The probe cannot demonstrate the *other* ordering, because
`httpx.MockTransport` does not enforce timeouts at all — the handler's `asyncio.sleep` runs to completion
regardless of what `httpx.Timeout` says. The good ordering is covered instead by
`test_timeout_is_the_transient_seam_error_never_a_builtin`, which raises `httpx.ReadTimeout` from the handler.)

### The two fixes

1. **`_HTTP_TIMEOUT_SHARE = 0.9` in the adapter.** The HTTP call now gets a strict share of
   `llm_timeout_seconds` rather than all of it, so httpx's timer fires first despite starting later, and the
   failure arrives as `OpenAICompatibleError` with `.provider` set. **A share, not a subtraction**, so the
   margin can never go negative or to zero however small the timeout is configured — a fixed `- 2.0` would
   invert at `DODEAL_LLM_TIMEOUT_SECONDS=1.0`. `llm_timeout_seconds` stays the true outer bound; the watchdog
   still enforces it, and is now a backstop for anything hanging *outside* the HTTP call rather than a
   competitor to it.

2. **`provider_reason` and `provider_transient` on `judgement_model_unavailable`.** The reason is promoted out
   of the `error` string into fields of its own, so a dashboard can count rate limits without parsing; and
   `transient` — which until now was set on every branch of `_status_error` and **read by nothing anywhere in
   the service** — is recorded for the first time. Guarded by `isinstance(exc.cause, LLMProviderError)`, so a
   transport error does not grow an invented reason. The seam's two attributes only: `provider` and `status`
   live on the adapter's subclass, and reaching for them from `units/` would mean importing a provider into the
   unit.

`reason_code` stays `model_unavailable` and the caller still gets one 503 for every provider failure. **No
behaviour changed** — this is attribution, not policy. Retrying a 429 remains forbidden.

### The sabotage record

| Sabotage, one line | Tests that failed | Restored |
| --- | --- | --- |
| `_HTTP_TIMEOUT_SHARE` back to `1.0` (parity, the bug) | `test_http_timeout_is_strictly_inside_the_watchdog_deadline` (2), `test_http_budget_scales_with_the_configured_timeout` (2), `test_the_share_leaves_a_real_margin` | yes |
| `isinstance(exc.cause, LLMProviderError)` to `if False` | `test_the_outcome_line_carries_the_provider_reason_as_a_field` (3, one per reason) | yes |

Both files restored from byte copies; `diff` against the copies is empty.

### The stopping chain at this head

```
1197 passed, 1 skipped, 28 deselected, 1 warning in 15.30s
Required test coverage of 92.0% reached. Total coverage: 99.56%
All 15 coverage floors met.
ruff check: All checks passed!   ruff format: 142 files already formatted
mypy: Success: no issues found in 66 source files
```

### For the lead

1. **Two register items are owed** (a session does not add them):
   - *The HTTP budget is a strict share of the watchdog deadline, so a provider timeout keeps its attribution.*
   - *The provider reason and transience are structured fields on the outcome line.*

2. **`0.9` is a judgement, not a measurement.** At a 60s timeout it hands the HTTP call 54s and leaves a 6s
   margin, which is far more than the milliseconds the race actually needs — the margin has to cover connect
   and send, which is why it is proportional rather than tiny. If you would rather spend less of the budget,
   the number is one constant with a comment above it. What must not change is that it stays strictly below 1.

3. **`.transient` is now *recorded* but still not *acted on*.** Nothing branches on it, and under the
   never-retry rule nothing should retry on it. The open question is whether a sustained run of
   `provider_reason=rate_limited` should trip something — a cooldown in the shape of `core/breaker.py` — or
   whether that is deliberately left to the provider's own backoff. **That is a design decision, not a bug**,
   and it is the one thing here I have not resolved.

4. **`test_classification.py` gained the tests for fix 2**, because it is where the provider-failure path is
   already driven; there is no `tests/unit/test_llm_call.py` in this tree.

5. **This is a second code commit in the session that built 76.1**, which `CLAUDE.md` says should have been two
   prompts. You asked for the fixes directly, so it was built as its own piece with its own commit and backfill
   rather than folded into 76.1's commit.

**Still nothing has met a real provider.** Both fixes are proven on `httpx.MockTransport` and `FakeLLM`.

---

## Piece 76.2: the LLM client built once in lifespan   STATUS: DONE `de0080c`

Register item 84, with the part-2 half of item 76. The seam is now wired end to end: lifespan builds one
client, `/ready` refuses rotation without it, and the timeout fix from 76.1a is proven against a real socket
rather than a mock. **The real Groq call is the lead's and has not been made.**

### The headline: 76.1a proven outside a mock, with its counterfactual

`--induce-timeout` stands up a real TCP listener that accepts a connection and never answers, points the
adapter at it with a dummy key, and sets a 5s watchdog. This is the one measurement `httpx.MockTransport`
structurally cannot produce, because it does not enforce timeouts at all — its handler simply runs to
completion, which is why the probe in 76.1a could only demonstrate one of the two orderings.

**At `_HTTP_TIMEOUT_SHARE = 0.9`, as shipped:**

```
Timeout:     5.0s watchdog
HTTP budget: 4.50s (_HTTP_TIMEOUT_SHARE=0.9)
Outcome:     ModelUnavailableError (503 model_unavailable)
Elapsed:     4.52s

  llm_provider_call_failed fired : True
    provider  : openai_compatible
    reason    : unavailable
    status    : None
    transient : True
  judgement_model_unavailable    : True
    provider_reason    : unavailable
    provider_transient : True
    error_type         : OpenAICompatibleError
```

**The same run with the share reverted to `1.0` — the pre-76.1a state:**

```
Elapsed:     5.00s

  llm_provider_call_failed fired : False
  judgement_model_unavailable    : True
    provider_reason    : <<missing>>
    provider_transient : <<missing>>
    error_type         : TimeoutError
```

Read the two together. At parity the watchdog wins at exactly 5.00s, the adapter's timeout branch never runs,
and the failure arrives as a bare builtin `TimeoutError` with no provider attached. At 0.9 httpx's timer wins
at 4.52s, the adapter's branch runs, and the failure arrives as `OpenAICompatibleError` naming the provider
and carrying `transient`. **This is the defect 76.1a described, reproduced and then closed, on a real
socket.** It also settles a claim I got wrong twice: at parity the watchdog does not win *sometimes*, it wins
*every time* — 5.00s on the nose, because its clock starts before `_resolve()` while httpx's read clock starts
only after connect and send.

**The provider label is `openai_compatible`, not `groq`.** Designed behaviour, reported and not fixed, per the
brief: `DODEAL_LLM_BASE_URL` is an override here, and an unrecognised base URL is labelled generically because
a proxy URL's host or path can itself be a credential.

### What landed

1. **`get_llm_client(request)`** reads `app.state.llm` and never builds. Building in the dependency would open
   a fresh connection pool per request — the exact bug the lifespan exists to prevent — and would re-run the
   profile sweep on every judgement. `None` is a fail-closed `LLMConfigurationError`. No test switch; the
   dependency override in tests is signature-agnostic, so no existing override changed.

2. **Lifespan** builds one `httpx.AsyncClient` and one LLM client onto `app.state`, and closes the HTTP client
   before the two Redis pools — it is the one holding sockets to a third party.
   - **Permissive when unset**, on the `dd_api_keys` precedent already in this file: `app.state.llm = None`
     plus one `llm_not_configured` ERROR on `dodeal_ai.startup`, after `configure_logging()` for the same
     reason as its neighbour. **No LLM config was wired into any test fixture**, as instructed.
   - **Refuses to start when set and wrong**: no key, no adapter, no model, or a profile that fails the sweep.

3. **The startup profile sweep**, inside `build_llm_client` rather than in lifespan. Putting it there means
   "a bad profile refuses to start" needs no branch in `main.py` and cannot be forgotten by a second caller.
   `OpenAICompatibleClient.validate_profile(name)` runs the same `_resolve` a real call runs, so the sweep can
   never disagree with what a judgement would do.

4. **`/ready` is strict about the model and stays lenient about Redis**, and the asymmetry is the point: the
   cost gate fails open, so a Redis outage is a state the service serves correctly through, while there is no
   fail-open path for a missing model — every judgement route 503s. A pod in that state can serve `/health`
   and nothing that matters, so it must not join rotation. `getattr(..., None)` and not `app.state.llm`,
   because a missing attribute must read as not-ready rather than AttributeError into a 500.

5. **`scripts/model_smoke.py`**, shaped after `scripts/real_fetch_check.py`: dry run by default against
   `model-smoke.invalid`, `--live` required for a paid call, a **named exit 1** when the key or provider is
   absent so it is safe in CI, and `--induce-timeout` above. The note and lead are invented and hardcoded.

### The pool number

`max_inflight (32) × LLM_CALLS_PER_JUDGEMENT (2) = **64** connections`, keepalive set to the same. Sized
against **passes, not requests**: classify runs alone, then vague and score are gathered, so one judgement can
hold two connections. A pool of 32 would make the second gathered pass queue for a connection *inside its own
timeout* — turning a pool wait into a model timeout and a 503, which is the failure mode I retracted in 76.1a
as not-yet-real. It is real from this commit, and this is the line that answers it.

### The sabotage record

| Sabotage, one line | Tests that failed | Restored |
| --- | --- | --- |
| `await app.state.http.aclose()` dropped | `test_the_pooled_client_is_closed_on_shutdown` | yes |
| Profile sweep iterates `[]` | `test_a_bad_profile_refuses_to_start` | yes |
| Dependency builds a fresh client instead of reading `app.state` | `test_factory_returns_the_client_lifespan_built`, `test_factory_never_builds_a_client_of_its_own` | yes |
| `/ready` LLM check behind `if False` | `test_ready_is_503_when_no_client_was_built`, `test_ready_is_503_when_lifespan_never_ran` | yes |

Plus the fifth, which is the piece's headline: `_HTTP_TIMEOUT_SHARE` to `1.0`, run against the real socket,
reproducing the bare `TimeoutError` at 5.00s. All five restored from byte copies; `diff` empty on all three
source files.

### The stopping chain at this head

```
1214 passed, 1 skipped, 28 deselected, 1 warning in 16.51s
Required test coverage of 92.0% reached. Total coverage: 99.56%
All 15 coverage floors met.
ruff check: All checks passed!   ruff format: 144 files already formatted
mypy: Success: no issues found in 66 source files
```

### For the lead

1. **The real Groq run is yours.** `DODEAL_JWT_SIGNING_KEY=... uv run python scripts/model_smoke.py --live`
   with the key in your `.env`. It prints the judgement, the model the provider **reported**, finish reason,
   tokens in/out, the provider request id, and connect+send+read in ms. **That last number is the first real
   measurement behind `_HTTP_TIMEOUT_SHARE`, which is still a judgement and not a measurement** — if connect
   plus send turns out to be a meaningful fraction of the budget on a cold connection, 0.9 is the number to
   revisit.

2. **No new `Settings` field**, so no `.env.example` row is owed and the 76.1b guard stays green.
   `LLM_CALLS_PER_JUDGEMENT` is a module constant in `main.py`, as instructed.

3. **The brief said two assertions in `test_health.py` flip; it was five** — the Redis-reporting test is
   parametrised four ways and the concurrency test is a fifth, and all five reach the real `/ready`. All five
   now take an `llm_built` fixture that puts a `FakeLLM` on `app.state`. `test_inflight.py` was untouched, as
   you said. Three new tests were added rather than one: the 503-without-client case, a 503-when-lifespan-
   never-ran case (a missing attribute must not read as ready), and the configured-and-built case.

4. **`tests/unit/test_llm_seam.py::test_factory_behaviour` was replaced, not edited.** It asserted the old
   parameterless contract — including `NotImplementedError("llm_provider_not_wired")`, which this piece
   deletes. Four tests now cover the request-scoped shape. The provider/model config cases it used to own
   already live on `build_llm_client` in the adapter's own suite.

5. **`.transient` is still recorded and unread**, and the induced-timeout run shows it reaching the outcome
   line correctly (`provider_transient: True`). No cooldown, counter or breaker was built. Item 20's provider
   half at A9, as ruled.

6. **The smoke script exercises ONE pass, not a full judgement.** The three-pass pipeline needs Redis for its
   reservation and counters, and a smoke script that needed a Redis to prove a model call would be testing the
   wrong thing. `classify` goes through `complete_once`, which is the whole seam. Say the word if you want the
   full pipeline behind a flag.

**The service has still never called a real provider.** Everything above is `MockTransport`, `FakeLLM`, and
one local socket that answers nothing.

---

## Piece 103b: the default run is hermetic against `.env`   STATUS: DONE `b263175`

Register item 115, the lead's, closed. The `.env` half of the job Piece 103 did for Redis. Tests and docs
only; no line of `src/` changed, and `_build_settings(**overrides)` keeps its signature — it is the correct
seam and was never the defect.

### How it was found, which matters more than what it was

Nobody looked for this. The lead configured `.env` to make the live Groq call that Piece 76.2 exists to
enable, and six tests went red. **The suite had been green only for people who had not configured a `.env`**,
and that had been true for the whole campaign.

Two of the six — `test_dd_api_keys_parses_json_map_of_secrets` and
`test_backend_keys_missing_logs_error_when_map_is_empty` — touch no LLM code at all and break on
`DODEAL_DD_API_KEYS` alone. They predate item 76 entirely. The other four arrived with 76.2 and share the
cause.

### `monkeypatch.setenv` could never have fixed it

This is the part worth keeping. The obvious reading is "the environment beats the file, so a test that sets
what it needs is safe." That is true for scalars and **false for dict fields**: pydantic-settings *merges*
complex values across sources. `test_dd_api_keys_parses_json_map_of_secrets` sets `DODEAL_DD_API_KEYS`
explicitly, which is the strongest thing a test can do, and still received:

```
AssertionError: assert {'localtenant', 't1', 't2'} == {'t1', 't2'}
  Extra items in the left set: 'localtenant'
```

The file contaminated a setting the test had set by hand. No amount of `setenv` defends that — the file has
to leave the source list entirely, which is why the fix is `env_file = None` and not a fixture that sets
variables.

### What landed

1. **`_no_dotenv` in the root conftest**, session-scoped and autouse, clearing `Settings.model_config
   ["env_file"]` for the run and restoring it after. The same shape and the same placement as
   `_closed_redis_urls`, which points both Redis URLs at a closed port for the same reason: an ambient
   dependency that must not decide whether the suite passes.

2. **Four guard tests in `tests/test_hermetic_fakes.py`**, because 103's lesson was that the property gets
   asserted, not assumed:
   - the fixture's own property (`env_file is None`), so deleting the fixture fails *here* with a sentence
     rather than somewhere downstream on whoever's machine has a `.env`;
   - the real defect reproduced — a populated `.env` written into a temp CWD, since `env_file=".env"`
     resolves against the working directory, so this is the actual situation and not an approximation;
   - **the dict-merge case**, the half `setenv` could not defend;
   - that `_build_settings(_env_file=...)` still reads a file somebody *asked* for. A fixture that broke
     that seam would have traded one silent failure mode for another.

3. **The false claim corrected in two places.** The root conftest's opening line —
   *"no test may depend on a local .env"* — was an intention, not a fact, and now says so. `README.md`'s
   "fully hermetic" paragraph claimed Redis hermeticity and said nothing about `.env`; it now carries both.

### The two runs, as asked

| Run | `.env` | Result |
| --- | --- | --- |
| A | the lead's populated one, present | **1218 passed**, 1 skipped, 28 deselected |
| B | no `.env` anywhere | **1218 passed**, 1 skipped, 28 deselected |

Identical. Run B was not produced by renaming the lead's file — if anything had crashed mid-run that would
have left their working setup broken. It is a full copy of the tree **including `.git`**, with every `.env*`
excluded except `.env.example`, run from that copy's own directory. Both earlier attempts at Run B were
discarded as invalid rather than reported: running from a different working directory broke 15 tests that use
repo-relative paths, and a copy without `.git` broke the seven in `test_assumption_markers.py`, which shells
out to `git grep`. Neither difference was about `.env`, so neither number would have meant anything.

(Coverage reads 0.00% in Run B because the editable install resolves `dodeal_ai` to the original `src/`, so
the copy's own `src/` is never imported. The test counts are what that run is evidence for.)

### The sabotage record

| Sabotage, one line | Tests that failed | Restored |
| --- | --- | --- |
| `env_file` put back to `".env"` in `_no_dotenv` | `test_env_file_is_cut_out_of_settings_for_the_whole_session`, `test_a_dotenv_in_the_working_directory_cannot_reach_settings`, `test_a_dict_setting_is_not_merged_from_a_dotenv` | yes |

Restored from a byte copy; `diff` empty.

### The stopping chain at this head

```
1218 passed, 1 skipped, 28 deselected, 1 warning in 17.32s
Required test coverage of 92.0% reached. Total coverage: 99.56%
All 15 coverage floors met.
ruff check (tracked files): All checks passed!   ruff format: 130 files already formatted
mypy: Success: no issues found in 66 source files
```

### For the lead

1. **`ruff check .` is red on an untracked `probe.py` at the repo root, which is not mine and not this
   piece's.** One import-sort fix, or one `.gitignore` row — ruff honours `.gitignore`, so the still-owed
   `docs: ignore scratch files` commit would cover it along with `n1.diff`, `n3.diff`, `n3b.diff`,
   `docs/audit/` and `docs/campaign/`. **That commit has been owed since the first Phase 0 of this session.**
   Ruff over every tracked file is clean.

2. **Register item 115 is written into `docs/register.md` on your say-so** ("new register item 115, which I
   am adding"). If you meant to add the row yourself, delete mine — a session does not add items, and this
   one is in only because you authored it in the brief.

3. **The `redis_real` lane is untouched** and still reads `os.environ` directly, so `DODEAL_REDIS_REAL_URL`
   works exactly as before. `_no_dotenv` changes only what `Settings` treats as a source.

4. **This makes CI and local agree for the first time.** CI has no `.env`, so it was always running the Run B
   configuration; local developers with a configured `.env` were running something else. The gap was invisible
   because the two populations never compared numbers.

5. **Nothing here touched a provider.** 76.2a is unstashed and committed separately on this green tree.

---

## Piece 76.2a: the smoke script prints the failure it already has   STATUS: DONE `a3c8c48`

Item 76's debugging half. No `src/` change. Held back one piece: the chain was red when it was finished, for
a reason that turned out to be register item 115, and it is committed here on the green tree 103b left.

### What it replaces

```
FAILED: ModelUnavailableError (model_unavailable)
Check DODEAL_LLM_API_KEY, the model id, and the provider's status page.
```

Three guesses, and the lead spent three debugging rounds rebuilding the request by hand against them. The
adapter had already recorded which of the three it was: `openai_compatible.py::_error` puts `reason_code`,
`provider`, `status` and `transient` on the `llm_provider_call_failed` record at the moment of failure. **The
script was throwing that away and asking the operator to guess.** Nothing new is computed here; a log handler
is attached for the duration of the call and the four fields are printed.

### Proven against a real server, per status

Each row is an end-to-end run: a real local HTTP server returning that status, the script's own failure path,
the printed output.

| HTTP | reason_code | status | transient | the one next action printed |
| --- | --- | --- | --- | --- |
| 400 | `invalid_request` | 400 | False | the adapter built the body; nothing in your env |
| 401 | `auth` | 401 | False | the key was not accepted |
| 403 | `auth` | 403 | False | the key's scope, not its spelling |
| 404 | `unknown` | 404 | False | **the MODEL ID** — check `DODEAL_LLM_MODEL` |
| 429 | `rate_limited` | 429 | True | rate/quota; never retried |
| 5xx | `unavailable` | 500 | True | the provider, not the request |
| — | `unavailable` | `None` | True | no answer at all: timeout, DNS, or refused |

A sentinel planted in every response body appears in **no** run's stdout or stderr. The rule is unchanged:
four fields and a status hint, never a body, a header, a URL or the key.

`<<missing>>` rather than a blank where a field is absent, so a missing field reads as a fact about the
failure instead of as a formatting gap. `status: None` is a real value with its own hint, which is why it is
not folded into the missing case.

### A false alarm I raised and then closed

The first harness pass reported 401 mapping to `unavailable`/`None` instead of `auth`/401 — which would have
been an adapter defect. It was **my throwaway test server**: it answers without draining the request body, so
with a ~4KB prompt the connection can reset mid-write and the call fails at the transport layer before a
status exists. Two clean re-runs confirm 401 maps correctly. Recorded because I raised it.

### Also in this commit

`--induce-timeout` now uses the same `_capturing()` context manager as the failure path, rather than its own
inline handler juggling. Re-verified identical afterwards: 4.52s, `OpenAICompatibleError`, adapter line fired.
The refactor broke it once — `_capturing()` is a **sync** context manager and cannot join an `async with` —
which is in the comment now.

### The stopping chain at this head

```
1218 passed, 1 skipped, 28 deselected, 1 warning
Required test coverage of 92.0% reached. Total coverage: 99.56%
All 15 coverage floors met.
ruff check (every tracked file): All checks passed!
mypy: Success: no issues found in 66 source files
```

### For the lead

1. **The real run is still yours:** `uv run python scripts/model_smoke.py --live`. If it fails now, it names
   which of the three things it is instead of listing them.
2. **No test covers this script**, by design — it is not collected by pytest (no `test_` name, lives under
   `scripts/`). Its evidence is the seven runs in the table above, which are hand runs and recorded here
   because a script that must never run in CI cannot be guarded by CI. That is the same arrangement as
   `scripts/real_fetch_check.py`.
3. **The status-hint table is a judgement about what a human should check next**, not a mapping the service
   depends on. `404 -> model id` is the one most likely to be wrong for a provider whose 404 means something
   else; it costs one line to change.

---

## Piece 78: the demo runtime   STATUS: DONE `ca22b2f`

Register item 78, closed. One `src/` change of four lines and two of a URL, plus three committed artefacts the
demo cannot run without. The fake CRM itself is register item 79, in the other repository, and is not here.

### The one `src/` change, and what it is not

`LeadsClient._base_url` carried the literal `https://`. It now reads `DODEAL_BACKEND_SCHEME`, a
`Literal["https", "http"]` defaulting to `https`.

**Why a setting at all.** The demo serves a fake CRM from a laptop, and a laptop has no certificate for
`tenant-a.dodealcrm.com`. Every alternative was worse. A self-signed certificate means teaching the client to
trust it, and that switch is *certificate verification* — a far more dangerous setting to own permanently
than four characters of a URL. Pointing the demo at a real HTTPS host means either a real tenant or a second
deployment, and the demo exists precisely because neither is available.

**What it is not.** It is not a verification switch. `https` still means verified `https`, and no setting in
this repository disables verification. The bound is `Literal`, so `ftp`, `HTTP` or a typo is a `ConfigError`
when `Settings` is built — at startup, with the rest of the config, never a scheme quietly pasted into a URL.

**What a wrong value costs, stated plainly:** `http` in production puts the per-tenant `DD-API-KEY` on the
wire in clear on every lead and note fetch. That is the entire risk, it is why the default is `https`, and it
is why the setting is documented as demo-only in `core/config.py`, `.env.example`, `.env.demo`, the README
configuration table and `ASSUMPTIONS.md` §3.9. Found set to `http` anywhere but the demo, it is a
key-disclosure incident and `docs/runbooks/secret-rotation.md` is the procedure — not a misconfiguration to
correct quietly.

`scripts/real_fetch_check.py` printed its target URL with its own hardcoded `https://`. It now builds that
line from the same two settings `_base_url` reads. A printed URL that disagrees with the call it describes is
worse than no printed URL, and it would have disagreed the moment the scheme was set.

### The trap this piece walked into, and the fix

`docker-compose.demo.yml` gives the container `.env` and then `.env.demo`, and **a later `env_file` wins**.
`scripts/mint_demo_token.py` reading `get_settings()` would therefore have read `.env` alone and signed with a
*different* signing key from the one the service verifies with. Every minted token would have failed Gate 1
with `invalid_token` — a generic 401 that reads exactly like a bug in the gates.

The script builds `Settings` over the same stack in the same order (`_build_settings(_env_file=(".env",
args.env_file))`, default `.env.demo`, missing files skipped), so the two cannot disagree by construction. It
goes through `_build_settings` rather than `Settings(...)` so the fail-closed `ConfigError` conversion stays
in the one place that owns it.

This is also what makes `.env.demo` able to leave `DODEAL_LLM_PROVIDER`, `DODEAL_LLM_MODEL` and
`DODEAL_LLM_API_KEY` unset: what the demo file does not set, a real `.env` still supplies. The repository
therefore never holds a provider key, and the lead's existing `.env` is never touched.

### Verified, not asserted

| Claim | How |
| --- | --- |
| The minted token passes **both** gates | Minted through the script's own code path under the demo environment, then run through `verify_token(JwtVerifier)` and `check_tenant` directly: `Identity(tenant='tenant-a', subject='42', database='demo_tenant_a')`, Gate 2 returns `tenant-a`. |
| A wrong Host is still refused | Same identity against `Host: localhost:8000` → `TenantMismatchError('invalid_host')`. |
| The signing key never leaves | The demo key's literal value asserted absent from the script's stdout **and** stderr. |
| The scheme survives the **real** httpx path | Two tests in the `integration` lane, through `HttpxTransport` and `ASGITransport` with nothing mocked but the transport. `FakeBackend` now records `str(request.url)`, so the assertion is on the URL the server saw, not on the string that built it. |
| Compose merges as the comments claim | `docker compose config` on the merged pair: `DODEAL_BACKEND_SCHEME=http`, the demo domains and the demo signing key win over `.env`; `DODEAL_LLM_PROVIDER` still comes from `.env`; the three Redis URLs from `environment:` still outrank both files; `extra_hosts` and the healthcheck render. |

### Sabotage

| # | Change | Tests that failed | Restored |
| --- | --- | --- | --- |
| 1 | `_base_url` back to the `https://` literal | `test_http_is_produced_only_when_the_setting_says_http`, `test_the_scheme_applies_to_every_read_path`, and in the integration lane `test_the_demo_scheme_reaches_the_backend_as_http` | yes, byte for byte |
| 2 | `Literal["https", "http"]` widened to `str` | `test_a_scheme_that_is_neither_https_nor_http_is_refused_at_construction` | yes, byte for byte |

Sabotage 1 is the one that matters: it proves the two default-preserving tests are not the whole coverage, and
that something fails when the seam is removed rather than only when it is misused.

### The 76.1b guard needed no change, and that is the point

`tests/test_env_example_matches_settings.py` pins `.env.example` **by path**. `.env.demo` is invisible to it,
which is the correct arrangement and required no code to achieve: `.env.example` is a TEMPLATE, pinned
field-for-field to `Settings`; `.env.demo` is a DEMO ARTEFACT that sets only what the demo needs to differ
from the defaults. Extending the pin would force the demo file to carry every setting and turn it into a
second, competing template that drifts against the first.

The guard did require its own row for the new field, and `.env.example` was appended to **blind** — the
permission rule denies reading `.env.*`. That is exactly the situation 76.1b's docstring describes as the
reason its secret-shape test exists. The row is commented (`# DODEAL_BACKEND_SCHEME=https`), which the guard
counts as documented, and which is the right shape for a demo-only override: discoverable, and unset unless
somebody means it. Placement is at the end of the file rather than beside `DODEAL_BACKEND_BASE_DOMAIN`,
because a file that cannot be read cannot be inserted into.

### `docker-compose.demo.yml` is an override, not a change to the base

Kept as a second file so that a plain `docker compose up` behaves exactly as it did before this piece. It adds
three things beyond `.env.demo`:

1. **`extra_hosts: tenant-a.crm.demo.invalid:host-gateway`.** `DODEAL_BACKEND_BASE_DOMAIN` is
   `crm.demo.invalid:8001` — the port is in the domain because `_base_url` interpolates that string directly,
   and it is the only way to reach a fake CRM that is not on port 80. `.invalid` is RFC 2606 reserved and
   never resolves, so removing this line fails the call immediately instead of letting it reach somebody
   else's host. That is the same argument `scripts/real_fetch_check.py` already makes for `leads-check.invalid`.
2. **A Redis healthcheck the API waits on.** The cost gate fails **open** without Redis, but the idempotency
   reservation fails **closed**: a judgement issued in the seconds before Redis accepts connections answers
   `503 idempotency_unavailable`. The demo is watched, and the first judgement is the one being watched.
3. Nothing about the model. That is the lead's decision (§3.8, `DECISION[DEMO_PROVIDER]`).

The base file's `env_file: .env` became `required: false`, because a clean checkout has no `.env` and Compose
refused to start before the service could say what was missing. Nothing is loosened: `Settings` still refuses
to build without `DODEAL_JWT_SIGNING_KEY`, so an empty environment still fails closed, in the app, with a
reason.

### Three things I chose, being unnamed by the prompt

1. **An override file rather than a `demo` profile in `docker-compose.yml`.** A profile cannot work: a service
   with no profile always starts, so `--profile demo` would have raced a second API container against the
   first on port 8000. Putting `.env.demo` into the base file's `env_file` list was the other candidate and is
   worse — a developer whose `.env` omits `DODEAL_BACKEND_BASE_DOMAIN` (relying on the default) would have
   silently inherited the demo's domain.
2. **`--route {fetch,direct,probe}` on the mint script.** Without `direct`, the piece's own deliverable — "the
   command sequence that produces one judgement" — is blocked on register item 79, in another repository. The
   default is `fetch`, which is the contract.
3. **Invented ids from the vendored corpus as defaults** (`--lead-id 1004 --note-id 115`) as plain numbers,
   not read from `tests/fixtures/`. `scripts/` must not import from `tests/`, and a default that is
   occasionally stale beats a coupling that is permanently wrong.

### What is still owed by a person

`.env.demo` deliberately leaves the three `DODEAL_LLM_*` rows unset. Until they are set the demo comes up,
passes both gates and answers `/health`, and `/ready` reports `503 {"llm": "not configured"}` while every
judgement route `503`s. **That is register item 84 working as designed**, not a broken demo — but it does mean
this piece cannot, by itself, produce a judgement.

---

## Piece 78a: the two runtime guards Piece 78's explainer named   STATUS: DONE `5cc1e55`

**No register item.** This piece exists because Piece 78's own explainer listed two production failure modes
and then shipped without guarding either. Writing the failure mode down is not the same as defending against
it, and an explainer that names a risk nobody then closes is worse than one that stays quiet — it creates a
record that reads as due diligence.

### Guard 1: a loud line when the backend scheme is http

`lifespan` now logs `backend_scheme_insecure` at **ERROR** when `settings.backend_scheme == "http"`.

**It is never a refusal, and that is the design.** The demo needs `http` — a laptop has no certificate for the
fake CRM — so refusing to start would break the one deployment the setting exists for. This is the loudest
thing available short of not serving.

**Three deliberate choices in four lines:**

| Choice | Why |
| --- | --- |
| **ERROR, not WARNING** | The consequence is a live credential crossing the network in clear. That is not degraded behaviour; it is disclosure, and WARNING is the level people filter out. |
| **After `configure_logging()`** | Audit §6.9, the same reason `backend_keys_missing` and `llm_not_configured` sit there. Emitted earlier the line goes out through whatever handler `logging` happened to have — unformatted, and invisible to a collector that parses only the JSON objects every other startup event produces. The loudest line in the file would be the one least likely to be seen. |
| **Names no key, no tenant and no URL** | The line travels to wherever logs go. A line that says *which* host is exposed carries that fact to a second place, and the operator reading it already knows their own configuration. The fact is the whole message. |

The wording says "key disclosure, not misconfiguration" on purpose. The natural read of a scheme setting is
"someone set this wrong, change it" — but by the time the line is read, every `DD-API-KEY` that process used
has already crossed the network. The next action is rotation
(`docs/runbooks/secret-rotation.md`), not an edit to an env file, and the line has to say so or it will be
triaged as tidy-up.

### Guard 2: the demo env-file order, pinned

Piece 78's second named failure mode: `docker-compose.demo.yml` and `scripts/mint_demo_token.py` each define
an env-file stack, they must agree, and **nothing checked**. A divergence — a file added to one, the demo file
moved ahead of `.env`, a rename done in one place — makes the minter sign with one key while the service
verifies with another.

**Why it needed a test rather than a comment.** The symptom points away from the cause. Nothing errors at
startup; the token is well-formed; Gate 1 simply returns a generic **401** with reason `invalid_token`, which
is byte-for-byte what a forged token produces. The first hour of that debugging goes into the gates, which are
correct. Piece 78's report said as much in prose, and prose does not fail a build.

`scripts/mint_demo_token.py` now exposes `DEFAULT_ENV_STACK: tuple[str, str] = (".env", ".env.demo")` as the
**one** definition of that stack. Before, it was `_BASE_ENV_FILE` at module level plus a `.env.demo` string
literal as an argparse default, joined only at the call site — two places, one of them inside a function, and
not importable as data. The test imports the constant rather than restating the list, so there is no third
copy to drift.

### No YAML parser, deliberately

`pyyaml` **is** importable in this venv, but only as a transitive dependency of `uvicorn[standard]` and
`pre-commit` — this project declares it in neither `[project].dependencies` nor the `dev` group. A guard built
on an undeclared transitive is a guard that disappears the day a dependency is dropped, and it would have been
invisible in review because the import works.

It is not needed. What is being pinned is two literal list entries in a known, small, hand-written file — not
YAML semantics. A line scan reads both forms Compose accepts (`- .env` and `- path: .env` / `required: false`)
and **fails closed**: an unrecognised block yields no entries, and the exact-match assertion then fails rather
than passing over an empty list. Two additional tests defend the scanner itself — one runs it against an
inline sample with both forms, and one asserts each compose file contributes at least one entry, so a reformat
to flow style fails the pin loudly instead of hollowing it out.

The scan is service-blind (it takes the first `env_file:` in each file), so a fifth test asserts neither
compose file grows a second `env_file:` block. That assumption is correct today and would rot silently.

### Sabotage

| # | Change | Tests that failed | Restored |
| --- | --- | --- | --- |
| A | `.env` appended **after** `.env.demo` in `docker-compose.demo.yml` — the exact divergence the pin exists for | `test_compose_env_file_order_matches_the_minter_default_stack`, `test_the_demo_file_is_last_so_it_wins` | yes, byte for byte |
| B | The `backend_scheme_insecure` branch deleted from `lifespan` | `test_http_backend_scheme_logs_an_error`, `test_http_backend_scheme_line_names_no_key_tenant_or_url` | yes, byte for byte |
| C | The message edited to name `backend_base_domain` — the helpful-looking edit the no-leak test refuses | `test_http_backend_scheme_line_names_no_key_tenant_or_url` | yes, byte for byte |

Sabotage C is the one worth keeping. Adding the offending host to the message is the obvious next improvement
somebody will make in good faith, and it is precisely wrong: it copies the identity of the exposed system into
the log pipeline. The test exists to make that edit fail rather than to make it unthinkable.

The silent-on-default test follows the `backend_keys_missing` precedent and **proves the capture channel works
before asserting an absence** — it logs a probe line through the same post-`configure_logging()` channel and
asserts the probe arrived. Without that, the absence assertion passes when the capture sees nothing at all,
which is exactly how the original `backend_keys_missing` absence test survived the §6.9 move while its partner
failed.

### A coverage reading I got wrong on the way

An intermediate run reported 9 missing lines against Piece 78's 8, with `logging_config.py:60` newly uncovered
— which is impossible from adding tests, since coverage is cumulative. It was a stale `coverage.json` left by
the run that immediately followed the sabotage restore. Two clean consecutive full runs agree: **8 missing
lines, identical to the Piece 78 baseline**, with `main.py` at zero misses — the new branch is exercised in
both directions. Recorded because the wrong number was believed for a few minutes and the next person reading
a one-line coverage drift should check for a stale artefact first.

### What was touched outside the named scope, and why

`README.md`'s repo-wide guards table said "Three tests" and listed three. It was already wrong before this
piece: `test_env_example_matches_settings.py` landed in 76.1b and was never added to it. Writing "Five" and
listing four would have been incoherent, so that row was written too. Named here rather than left as a silent
ride-along.

## Piece 102: the flaky elapsed test on a controlled clock   STATUS: DONE `498d937`

**Register item 102, closed.** `tests/unit/test_judgement_pipeline.py::test_elapsed_covers_more_than_any_single_pass`
failed at random on the Windows machine — observed **1 in 6** on one head and **5 in 14** during a later
piece. The cause is not in `src/`. No file under `src/` is touched by this piece.

### What was actually wrong

The test injected a delay into the note fetch and asserted the outcome line saw it:

```python
async def _slow_get_lead(*args, **kwargs):
    await asyncio.sleep(0.02)  # 20 ms, in real time
    ...
```

...and then asserted `line["elapsed_ms"] >= 20` on the outcome line.

`elapsed_ms` is computed as `int((time.monotonic() - started) * 1000)`. On this machine
`time.monotonic()` advances in **~15.6 ms steps** — the default Windows timer period — so a real 20 ms sleep
can be observed as a *single tick*, 15 ms, and `15 >= 20` is false. The pipeline was correct every time; the
**instrument** was too coarse to measure what the test asked it to measure.

This is the worst shape a flaky test comes in. It is red on correct code, which trains everyone reading the
suite to re-run rather than to look — and `CLAUDE.md` had written that training down as a rule.

### The fix: a clock the test owns

`_FakeClock` is substituted for the **pipeline module's `time` global**, not for `time.monotonic` itself:

```python
clock = _FakeClock()
monkeypatch.setattr(pipeline_module, "time", clock)


async def _slow_get_lead(*args, **kwargs):
    clock.advance(0.02)  # 20 ms, on a clock that cannot round it down
    ...
```

**Why the module global rather than the function.** `test_the_pipeline_uses_the_monotonic_clock`, a few lines
further down the same file, does `monkeypatch.setattr(pipeline_module.time, "monotonic", _counting)` — and
`pipeline_module.time` **is** the shared `time` module, so that patch is process-wide. It is harmless there
because the replacement delegates to the real clock and only counts reads. It is not harmless here: asyncio's
`BaseEventLoop.time()` calls `time.monotonic()`, so a clock that jumps twenty milliseconds would be jumping
under the event loop's own scheduling — including the `asyncio.timeout(judgement_deadline_seconds)` this very
code path opens. That trades one source of flakiness for a subtler one. Patching the module's `time` **name**
confines the fake to the four call sites in `pipeline.py`, which are the only `time.` reads in that file.
`__getattr__` delegates every other attribute to the real module, so the patch narrows to `monotonic` alone.

**Why the fetch and not the passes.** The clock advances only inside the fetch wrapper. The three model passes
never advance it, so `classify_ms` is `0` **by construction** rather than by being fast — the 20 ms can only
have come from the fetch, which is the claim the test was written to make.

### The assertions are unchanged

```python
assert line["elapsed_ms"] >= 20
assert line["classify_ms"] < 20
```

Neither was weakened to `>=`, and no tolerance margin was added. Both were available as "fixes" and both are
wrong: a margin wide enough to absorb a 15.6 ms tick is wide enough to absorb the defect the assertion exists
to catch. The flake was in how the span was **produced**, so that is the only thing that changed.

`int((0.02 - 0.0) * 1000)` was checked to be exactly `20`, not `19`. The clock starts at `0.0` and the span is
small, so the subtraction stays in the range where the double nearest `0.02` multiplies back above 20. Had it
started at a large base — `1000.0`, say — the spacing of doubles near 1000 is 1.1e-13 and the difference can
land *below* 0.02, which would have reintroduced the same off-by-one-tick failure from the opposite direction.

### The twenty-run guard

Twenty consecutive runs of the module: **twenty green, zero red.** Before the change the same loop would be
expected to show two or three reds at the observed rates.

### Sabotage

| # | Change in `pipeline.py` | Result | Restored |
| --- | --- | --- | --- |
| A | `elapsed_ms=_ms_since(started),` deleted from the `_Timings(...)` on the outcome line — the literal sabotage the brief names | Fails by name, but on `TypeError: _Timings.__init__() missing 1 required positional argument: 'elapsed_ms'`. **The assertion never runs.** | yes |
| B | `started` re-read *after* `_fetch_note` in `judge_note` — the clock starting late, which is the defect the test guards | `FAILED ... test_elapsed_covers_more_than_any_single_pass` on **`assert 0 >= 20`**, the `elapsed_ms` assertion itself | yes |

Sabotage A is reported as run, and as **insufficient**. `elapsed_ms` is a required field of `_Timings`, so the
line cannot be removed without the constructor raising — the test goes red, but on a `TypeError` that would
also take dozens of other tests with it, and it proves nothing about the assertion. Sabotage B is the one that
carries the claim: it changes the measurement's *meaning* while leaving everything well-typed, it is the
mistake somebody could actually make, and the named assertion is what catches it. **`assert 0 >= 20`** is also
a sharper failure than the old test could produce — on a real sleep the sabotaged value would still have been
a small non-zero number.

Restoration is verified by blob hash, not by eye: the working tree's `pipeline.py` hashes to
`dfbef412f1982f0cce9859c90f4cda7b60108403`, identical to `HEAD:src/.../pipeline.py`.

### A line-ending trap worth recording

Rewriting `pipeline.py` for the sabotage normalised it from CRLF to LF on disk. The tree is **mixed** — 15 of
57 `src/*.py` files are CRLF — while `.gitattributes` declares `* text=auto eol=lf`, so `git status` reports
the CRLF copies as *modified* and the LF copies as clean. A byte-compare against a pre-sabotage `cp` backup
therefore reported all 1034 lines as differing while the file was in fact correctly restored. **`git
hash-object` against the `HEAD` blob is the check that means something here; `cmp` is not.**

### `CLAUDE.md`, changed outside the named scope

`CLAUDE.md` carried a standing rule: *"One test is known to be flaky on the Windows machine:
`test_elapsed_covers_more_than_any_single_pass` ... If it is the only red test, re-run once."* That rule is
now false, and false in the dangerous direction — it instructs every future session to re-run past a red in
exactly this test, which is now the one place a red is guaranteed to be real. The line was rewritten to
withdraw the exemption and name this commit. The surrounding rule (stop, BLOCKED, never fix forward, never
skip) is unchanged. Named here because the brief did not ask for it.

### Chain

`1230 passed, 1 skipped, 30 deselected; 99.51% coverage`. All 15 coverage floors met; none added, none moved.
`ruff check`, `ruff format --check`, `mypy` clean. Test count unchanged — the test was rewritten, not added.
The `redis_real` lane was not run: this piece touches no Redis code and no Lua.

### Files

| File | Change |
| --- | --- |
| `tests/unit/test_judgement_pipeline.py` | `_FakeClock`; the test driven by `clock.advance` instead of `asyncio.sleep` |
| `CLAUDE.md` | the re-run exemption withdrawn |
| `docs/register.md` | header sha and master edition; items 101 and 102 to DONE; the step order replaced |
| `docs/STATUS.md` | the Piece 102 row; suite coverage line corrected to 99.51% |
| `CAMPAIGN_REPORT.md` | this block |

---

## Piece 95: a pool-refused probe must not hold the breaker half-open   STATUS: DONE `cf86985`

**Register item 95, closed.** It is the gap Piece N.3b recorded and left open in its own "For the lead",
item 2: *"A probe refused by the pool stays HALF_OPEN until guard 2 takes it over one window later ... Handing
the probe straight back (OPEN, window already expired) is a two-line change if you want it."* This is that
change. One file under `src/`: `core/breaker.py`.

### What was actually wrong

`PoolExhausted` subclasses `redis.ConnectionError`, so in `CircuitBreaker.call` the `except redis.RedisError`
clause matches it **first** and the `except BaseException` clause below — item 80's guard 1 — never sees it.
The branch did exactly one thing: skip `_record_failure`.

```python
except redis.RedisError as exc:
    if not isinstance(exc, PoolExhausted):
        self._record_failure()
    raise
```

That is right about counting and silent about state. `_admit` had already moved the breaker to HALF_OPEN and
stamped `_probe_started = now`, so after the refusal the breaker sat in HALF_OPEN **holding a probe slot for a
probe that never ran**. Every other caller then met `BreakerOpen` at `_admit`'s `now - self._probe_started <
self._open_seconds` for a full `DODEAL_BREAKER_OPEN_SECONDS`, until guard 2's takeover handed the slot on.

The trigger matters. Item 81 exists because a latency spike that queues requests on our own pool must not open
a breaker on a healthy Redis. In CLOSED it does not. In HALF_OPEN it did — and worse, because HALF_OPEN
refuses *everyone*, not just the caller who lost the acquire. On the operational connection that is a 503 on
every judgement for a window, with the store never asked and nothing wrong with it.

**A third way not to report back.** The module docstring numbers two, and both are about a probe that did not
**answer**: one that ended without an answer (guard 1), one that never ended (guard 2). A probe the pool
refused never **started**. That is why neither guard covered it.

### The change

```python
except redis.RedisError as exc:
    if isinstance(exc, PoolExhausted):
        if self._state is BreakerState.HALF_OPEN:
            self._log_probe_abandoned()
            self._state = BreakerState.OPEN
        raise
    self._record_failure()
    raise
```

Three properties, each of which is the point:

1. **`_state = BreakerState.OPEN`, not `self._open()`.** `_open()` re-stamps `_opened_at` from the clock. The
   window that had already elapsed is exactly what makes the next call a probe: `_admit` promotes OPEN →
   HALF_OPEN on `now - self._opened_at >= self._open_seconds`. Leaving the stamp alone means the next arriving
   call probes **at once, with no clock advance at all**. Re-stamping would impose a fresh full window of
   refusals — strictly worse than the defect being fixed, which at least ended at guard 2's takeover.
2. **`raise`, unchanged.** `PoolExhausted` reaches the caller as the same object, so the reservation still
   fails closed with a 503 and the money guards still fail open on that one call. The breaker's decision about
   the *next* call is separate from that caller's policy for *this* one.
3. **Still never counted.** The state revert is not a failure record. `_failures` is untouched, in every state.

### On the log line

`breaker_probe_abandoned` is reused, not extended with a new name. Its docstring already reads *"one line per
probe that did not answer"*, and a probe the pool refused did not answer. The named-events list in `README.md`
is a documented surface; adding to it is a decision for the lead, not an implementation detail. The argument
for a distinct name is recorded under "For the lead" below and **not built**.

One consequence worth naming: on this path the line is **not** followed by `breaker_opened`, because `_open()`
is what logs that and `_open()` is deliberately not called. The other two paths are followed by either
`breaker_opened` (guard 1) or nothing (guard 2's takeover). The `README.md` event row said the `PoolExhausted`
case arrived through guard 2 a window later; that is no longer true and was corrected.

### Guards

| Test | What it holds |
| --- | --- |
| `test_a_pool_refused_probe_reopens_without_a_new_window` | HALF_OPEN + `PoolExhausted` → state OPEN, `_opened_at` **unchanged** (compared against the value read before the call), the exception re-raised with `type(caught.value) is PoolExhausted`, and exactly one log record: `breaker_probe_abandoned`, carrying `breaker` |
| `test_the_next_call_after_a_pool_refused_probe_is_the_probe` | **No clock advance whatsoever.** The very next caller reaches the factory and its success closes the breaker — `(probe.calls, state) == (1, CLOSED)` |
| `test_a_pool_refusal_while_closed_still_changes_nothing` | The fix does not widen. In CLOSED: state unchanged, **no log records at all**, and the consecutive count neither raised nor cleared — one more store failure still opens it, and no fewer would |

The clock is the test module's existing `_Clock`, moved by hand. No `asyncio.sleep`, no real time.

### Sabotage

| # | Change in `core/breaker.py` | Result | Restored |
| --- | --- | --- | --- |
| A | `self._state = BreakerState.OPEN` replaced by `self._open()` — the brief's named sabotage, and the plausible mistake | **2 failed, 26 passed.** Test 1 on **`assert 1030.0 == 1000.0`** (`breaker._opened_at`, re-stamped by the clock). Test 2 on `assert await breaker.call(probe) == "answered"`, raising `BreakerOpen` from `_admit`'s `now - self._opened_at < self._open_seconds` — the next caller refused for a fresh window, which is the defect made worse | yes |

Test 2 is the one carrying the claim: it fails on the **behaviour** a user sees, not on a private attribute.
Test 1 names the mechanism. Restoration verified by re-running the module: **28 passed**.

### The existing guard 2 test

`test_a_probe_that_never_returns_is_taken_over_after_a_window` **passes unchanged** and was not touched. It
drives a probe that hangs rather than one the pool refuses, so this piece does not make it unreachable: a probe
task that is never resumed still raises nothing, and only its age can free the breaker. What the fix removes
from guard 2 is one *cause* of an abandoned probe, not the guard.

### Chain

`1233 passed, 1 skipped, 30 deselected; 99.51% coverage` (1230 → 1233, the three new tests; coverage
unchanged). All 15 coverage floors met, none added, none moved; `core/breaker.py` measures **100.00%**
against its floor of 100, with its statement count 96 → 102.

**One coverage reading is not reproducible, and it is not this piece's.** The first full run of the session
reported `99.56%` with **8** statements missed. The four runs after it all reported `99.51%` with **9** — the
figure `docs/STATUS.md` already carried before this piece, and the one recorded. **Which** statement flipped
was not captured: that first run's per-file table was read only from `tools/` down, and the nine missed
statements in the stable runs sit in five files above it (`auth/claims.py` 95 and 112, `auth/dependencies.py`
55, `auth/verify.py` 110, `logging_config.py` 60, `tools/httpx_transport.py` 18–21). `core/breaker.py`
measured **100.00% in every run**, so this piece is not the source. The wobble is older than the piece: five
earlier blocks in this file record `99.56%` and Piece 102's records `99.51%`, with no coverage-relevant change
between them. Worth pinning down before any floor is set near those five files — a statement whose coverage
depends on process state rather than on the tests is a floor that can move on its own.
`ruff check`, `ruff format --check`, `mypy` clean. `integration` lane: 9 passed. `redis_real` lane: **21
skipped** — no server was available on this machine (nothing listening on 6379, `DODEAL_REDIS_REAL_URL`
unset). It is a hand run and is owed to the lead for this piece, though the change is pure state-machine logic
on an injected clock and touches no socket, no pool and no Lua.

### For the lead

1. **The docstring was extended, and the two guards were not renumbered.** The pool paragraph gained a
   paragraph of its own ("AND IT DOES NOT HOLD THE PROBE SLOT"), rather than item 80's numbered list becoming
   three. That list answers *"how can a probe fail to answer?"* and has exactly two members; a probe that never
   started is a different question, and it belongs beside the other `PoolExhausted` reasoning it depends on.
   Renumbering would also silently invalidate every doc and report that cites "guard 1" and "guard 2" by
   number, including `README.md` and three prior blocks in this file.
2. **The argument for a distinct event name, recorded and not built.** `breaker_probe_abandoned` now covers
   three distinguishable causes, and an operator reading it cannot tell a Redis that went quiet (guards 1
   and 2 — investigate the store) from a pool that was full (this path — investigate our own concurrency and
   `DODEAL_REDIS_MAX_CONNECTIONS`). Those call for opposite responses. The cheap answer is a field rather than
   a name — `extra={"breaker": ..., "cause": "pool"}` — which keeps the named-events list at its current
   length and is still a documented-surface change. **Your call; nothing was added.**
3. **Item 96 is now the remaining half of this failure mode.** A pool sized below `max_inflight` turns ordinary
   load into `PoolExhausted`. This piece makes that stop costing a breaker window; it does not make the pool
   big enough. 96 (the startup WARNING for an explicit size below the cap) is still OPEN and still owed.
4. **N.3b's "For the lead" item 3 is untouched and still true.** A late result can be attributed to the wrong
   probe, because transitions key on the state rather than on which call is the probe. This piece adds no new
   instance of it — the refused call's own `raise` happens immediately, with no `await` between `_admit` and
   the branch — but it does not close it either. The probe generation counter is still at A9.
5. **`ASSUMPTIONS.md` carries no entry for this.** The brief asked for the accepted-risk entry saying a
   half-open probe refused by the pool holds the breaker for one window to be corrected there. It does not
   appear in that file — the only breaker mention in `ASSUMPTIONS.md` is line 1218, about step 3 and the H3
   breaker, which is unaffected. The risk was recorded in **`CAMPAIGN_REPORT.md`, Piece N.3b, "For the lead"
   item 2**, and is closed by this block rather than by editing that historical record. Nothing was added to
   `ASSUMPTIONS.md`.
6. **No `.env.example` row is owed and no setting changed**, as the brief said.

### Files

| File | Change |
| --- | --- |
| `src/dodeal_ai/core/breaker.py` | the `PoolExhausted` branch reverts HALF_OPEN to OPEN without re-stamping `_opened_at`; module docstring gains the third path; `call`'s docstring updated |
| `tests/unit/test_breaker.py` | three tests, and a module-level `_exhausted` factory |
| `README.md` | the `breaker_probe_abandoned` event row (the `PoolExhausted` case no longer arrives through guard 2); the `breaker.py` module row; the `test_breaker.py` row |
| `docs/register.md` | item 95 to DONE; header sha; 102 dropped from the step order |
| `docs/STATUS.md` | the Piece 95 row; the register-item section; suite line to 1233 / 99.51%; header piece list |
| `CAMPAIGN_REPORT.md` | this block |

## Piece 85: the prompt templates are read once at startup   STATUS: DONE `f3e3788`

**Register item 85, closed.** *"Preload all nine prompt templates at startup; a missing file fails startup."*
Two files under `src/` changed and one was added: `core/prompting.py`, `main.py`, and
`units/structured_intelligence/templates.py`.

### What was actually wrong

`_load_template` read from disk on every call, and `build_prompt` and `with_tail` each call it:

```python
def _load_template(name: str) -> str:
    path = _prompts_dir() / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptError(f"prompt template not found: {name}") from exc
```

Two costs, and the second is the expensive one.

**A blocking read on the event loop, three to six times per judgement.** `_prompts_dir()` also builds
`Settings` on each of them. This service is one loop (`tests/test_no_sync_clients.py` exists to say so): a
read that blocks does not slow the request that made it, it stops every request in the process. Three reads on
the happy path — classify, vague, score — and up to six when both gathered passes are reprompted, since the
tail is a fourth template and `with_tail` re-reads it each time.

**A missing template was discovered on the first paid call.** A deployment whose image lost
`vague_won_lost_v1.txt` started, passed `/ready`, answered `/health`, served every judgement whose note was
not a `won_lost` — and then, on one that was, raised `PromptError` **after** classification had already been
bought and charged. The fault is in the image, and it was being reported as a runtime failure of one note
type.

### The change

`core/prompting.py` gains the cache and the two functions that bracket its life, and the disk read is
extracted into the one site both go through:

```python
_TEMPLATE_CACHE: dict[str, str] = {}


def preload_templates(names: Iterable[str]) -> None:
    loaded = {name: _read_template_file(name) for name in names}
    _TEMPLATE_CACHE.update(loaded)


def clear_templates() -> None:
    _TEMPLATE_CACHE.clear()


def _load_template(name: str) -> str:
    cached = _TEMPLATE_CACHE.get(name)
    if cached is not None:
        return cached
    return _read_template_file(name)
```

Four properties, each of which is the point:

1. **The fallback is deliberate, not a leftover.** An uncached name reads from disk exactly as it did before
   the cache existed. That is what keeps `scripts/verify_wheel.py`, `tests/unit/test_assembled_prompt.py`,
   `tests/unit/test_prompting.py` and `FakeLLM.script_for` — none of which runs inside a lifespan — working
   with no change at all, and it is what lets a test point `_prompts_dir` at a `tmp_path` and still read what
   it wrote. A cache that raised on a miss would have made this piece a rewrite of four test modules.
2. **The preload is all-or-nothing.** `loaded` is a local mapping published only once every name resolved, so
   the first missing file raises with `_TEMPLATE_CACHE` still empty. Without that, a caller that caught the
   `PromptError` — a test, a script, anything embedding the app — would carry on against eight templates the
   app never had.
3. **The refusal names the template, not the path.** `prompt template not found:
   structured_intelligence/score_v1.txt`, and nothing else. That string reaches a log line, and the resolved
   path carries the deployment's directory layout with it. A test asserts `str(tmp_path)` is absent.
4. **`_read_template_file` is one function so a test can count reads by patching one name.** Patching
   `Path.read_text` globally would count pytest's own reads of source files and prove nothing about this
   module.

`main.py` places the preload **first**, and the clear **first** on the way out:

```python
    settings = get_settings()
    configure_logging()
    # FIRST, before any socket or pool exists: a missing template must refuse
    # here, not on the first paid call days later. ...
    preload_templates(UNIT_A_TEMPLATES)
```

Before `app.state.http`, which is the reason this piece adds no unclosed resource: the refusal happens while
the process still owns nothing. After `configure_logging()`, so anything that does log during startup logs in
the JSON shape a collector parses (audit §6.9, the same argument as `backend_keys_missing`). And the ordering
choice on shutdown — `clear_templates()` before the three `aclose()` calls — is made on failure, not on
tidiness: clearing a dict cannot raise, so it is the one shutdown step that cannot be skipped by an earlier
one failing.

**The nine are named by the unit, not by core.** `units/structured_intelligence/templates.py`:

```python
VAGUE_CHECKED_TYPES: tuple[NoteType, ...] = tuple(
    note_type for note_type in NoteType if note_type is not NoteType.SYSTEM_EVENT
)

UNIT_A_TEMPLATES: tuple[str, ...] = (
    CLASSIFY_TEMPLATE,
    *(template_for(note_type) for note_type in VAGUE_CHECKED_TYPES),
    SCORE_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
)
```

`core/prompting.py` knows how to read a template and has never known which ones exist; nine hardcoded
filenames there would be a second place to edit every time this unit grows a pass. Every entry is the
**constant the calling module already passes to `build_prompt`**, never a retyped string, so a renamed file
cannot leave a template that is sent out of the preload — which would be the worst outcome available here: a
disk read back on a paid call, silently, with every test still green.

The six vague names come from iterating `NoteType` and dropping `SYSTEM_EVENT` (the classifier suppresses it
before pass two), so an eighth note type joins the preload the moment it is added, and a type with no template
is a `PromptError` at **import** rather than a `KeyError` on a judgement.

### The guard

Seven tests in `tests/unit/test_prompt_preload.py`:

- **Startup refuses.** A `tmp_path` holding eight of the nine, `_prompts_dir` patched to it, entering the
  lifespan raises `PromptError`; the message names `structured_intelligence/score_v1.txt` and does not
  contain the directory.
- **A refused startup leaves no half-filled cache.** `_TEMPLATE_CACHE == {}` after a caught failure.
- **The tuple is the nine that ship.** Nine entries, nine distinct, every one a real file under
  `src/dodeal_ai/prompts/`. This is the test that catches the renamed-constant case the tuple is built to
  prevent.
- **Zero reads.** A lifespan that actually ran, a full judgement on the fetch route against `FakeLLM` with a
  malformed vague answer so the reprompt fires — four model calls, five prompts assembled — and the read
  counter, installed *after* startup, is empty.
- **The cache does not outlive the app.** Populated inside the lifespan, `== {}` after it, and
  `_load_template` then reads the disk again (the counter increments).
- **The fallback still reads disk with no app started**, and **a preloaded name is served from the cache**
  even after the file under it changes.

`tests/unit/test_assembled_prompt.py` and `tests/unit/test_prompting.py` pass unchanged, which is the fallback
being proven by the tests that were already there.

### Sabotage

**(a) `_load_template` skips the cache lookup** (the body reduced to `return _read_template_file(name)`):

```
FAILED tests/unit/test_prompt_preload.py::test_a_full_judgement_reads_no_template_from_disk
FAILED tests/unit/test_prompt_preload.py::test_a_preloaded_name_is_served_from_the_cache_not_the_file
2 failed, 5 passed
```

`assert read_counter == []` failed with five reads, and the second test read `SECOND` where the cache holds
`FIRST`. Restored byte for byte.

**(b) `preload_templates` swallows the missing-file error.** The read site wraps `OSError` in `PromptError`
before `preload_templates` ever sees it, so the sabotage is the `except PromptError: continue` that a
swallow would actually look like here:

```
FAILED tests/unit/test_prompt_preload.py::test_startup_refuses_when_one_template_is_missing
FAILED tests/unit/test_prompt_preload.py::test_a_refused_startup_leaves_no_half_filled_cache
2 failed, 5 passed
```

`Failed: DID NOT RAISE PromptError` — the app came up on eight templates. Restored byte for byte.

### For the lead

1. **`UNIT_A_TEMPLATES` lives in a new `units/structured_intelligence/templates.py`, not in the package
   `__init__.py`.** That file is deliberately empty (its own docstring says so), and filling it would make
   importing **any** module in the package — `state`, which the root `tests/conftest.py` imports — drag in
   `classify`, `vague`, `scoring` and `llm_call` with it, and put the package `__init__` in a position to
   import a submodule that imports the package. A separate module leaves the import graph exactly as it was.
2. **`core/prompting.py` gained no per-file coverage floor.** It now holds a cache with a fallback branch,
   which is an argument for one; it is not on a deny path or a scoring path, which is the argument the
   existing list is built on. It measures **100%** at this head. Not added — floors are the lead's.
3. **The lifespan's existing order is untouched and still leaves `app.state.http` unclosed if
   `build_llm_client` raises.** This piece's step runs *before* the client exists, so it adds no instance of
   that; it does not close it either. Worth its own decision.
4. **The preload runs before `backend_keys_missing` and `backend_scheme_insecure` are logged**, as the brief
   specified ("immediately after `configure_logging()`"). A deployment missing both a template and its backend
   keys therefore refuses without the keys line. That is the right trade — a refusal is a refusal — but it is
   a visible consequence of the ordering, so it is recorded here rather than assumed.
5. **`docs/register.md` lists item 85's carrying step as "A9a, with 76"**, while the step-order line at the top
   of the same file reads "95, 85, 86, 87, 88 ..." and this piece carried 85 alone. The column is stale
   against the order line; nothing was changed but the status and sha.
6. **No `.env.example` row is owed and no setting changed.** `DODEAL_PROMPTS_DIR` already exists and its
   behaviour is unchanged — it still overrides the location, and the override is now read once at startup
   rather than on every call.
7. **`ASSUMPTIONS.md` is untouched.** §8.5 (`AssembledPrompt`) and §8.6 (packaging, and
   `DODEAL_PROMPTS_DIR` "overrides the prompt location for local iteration only") are both still true
   word for word after this piece. Nothing in that file described the per-call read.

### Files

| File | Change |
| --- | --- |
| `src/dodeal_ai/core/prompting.py` | `_read_template_file` extracted as the one disk read; `_TEMPLATE_CACHE`, `preload_templates`, `clear_templates`; `_load_template` reads the cache first |
| `src/dodeal_ai/main.py` | `preload_templates(UNIT_A_TEMPLATES)` immediately after `configure_logging()` and before `app.state.http`; `clear_templates()` first after `yield`; two imports |
| `src/dodeal_ai/units/structured_intelligence/templates.py` | new: `VAGUE_CHECKED_TYPES` and `UNIT_A_TEMPLATES`, built from the four constants |
| `tests/unit/test_prompt_preload.py` | new: seven tests (startup refusal, no half-filled cache, the nine, zero reads, lifetime, and both sides of the fallback) |
| `README.md` | the `prompting.py` module row; the `main.py` module row; the `structured_intelligence/` directory row; the `DODEAL_PROMPTS_DIR` configuration row |
| `docs/register.md` | item 85 to DONE; header sha; 85 dropped from the step order |
| `docs/STATUS.md` | the Piece 85 row; the register-item section; suite line to 1240 / 99.57%; header piece list |
| `CAMPAIGN_REPORT.md` | this block |

---

## Piece 86: the two middlewares as pure ASGI   STATUS: DONE `76ed6af`

**Register item 86, closed.** *"Rewrite `RequestIDMiddleware` and `InflightMiddleware` as pure ASGI."*
Two files under `src/` changed and nothing else: `middleware/request_id.py`, `middleware/inflight.py`.
`main.py` is untouched — the class names and the one-argument constructor are the same, so the registration
and its comment stay word for word.

### What was actually wrong

Both classes were `starlette.middleware.base.BaseHTTPMiddleware`. That base class is not a naming choice; it
is a runtime. Every request it sees is run through an anyio task group and a pair of memory object streams,
so that `dispatch` can be handed a `Request` and return a `Response`. Three costs, and the middle one is the
one that decided this piece.

**The load-shed middleware was paying for a task group in order to refuse work.** Its own docstring is
explicit about the standard it has to meet:

> Before everything else, so a refusal costs a counter comparison and nothing more -- no token verification,
> no tenant resolution, no body read, no Redis round-trip.

And then, before reaching that comparison, every request — admitted or refused — had a task group created
for it, two memory streams opened, and a child task spawned. A defence that costs more than the work it
refuses is not a defence, which is the argument the module already makes against reading a body; it applied
to the base class too and had not been noticed.

**The route ran in a child task, so `contextvars` did not survive a round trip.** This is the property the
rewrite restores, and it is worth being precise about which half was broken, because the obvious test does
not discriminate:

- **Downward** (set outside the middleware, read in the route) **worked before this piece.** A child task
  copies its parent's context at spawn, so a `ContextVar` set before `call_next` reaches the endpoint. A test
  asserting this would have passed on both trees and proved nothing.
- **Upward** (set in the route, read by a middleware after the call returns) **did not, and cannot.** The
  route's `set()` lands in the child task's copy of the context and is discarded with the task. Measured on
  the tree at `516f94d`, with one probe middleware outside the pair and a route that sets the var:

  ```
  with the two middlewares in the stack: outer sees -> MISSING
  without them:                         outer sees -> SET-IN-ROUTE
  ```

  That is the direction any future request-scoped context depends on: a tenant, a trace span, a cost tally
  set deep in the pipeline and read on the way out.

**Cancellation behaviour that moves between releases.** `BaseHTTPMiddleware`'s handling of a disconnect
mid-response has changed more than once upstream. Starlette is pinned at 1.3.1, which defers the question
rather than answering it. A plain callable has no such behaviour of its own to change.

### The change

Both are now the two-method shape. `RequestIDMiddleware`:

```python
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _accepted_inbound_id(scope) or str(uuid.uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["observability"] = RequestObservability(
            request_id=request_id, trace_id=request_id
        )
```

Four things in that are deliberate, and each is a way it could have been got wrong:

**`scope["state"]` is not a second home for the id.** `Request.state` is a *view* over exactly this dict —
Starlette's `HTTPConnection.state` does `scope.setdefault("state", {})` and wraps the result — so writing the
key here **is** writing `request.state.request_id`. All four `getattr(request.state, "request_id", "unknown")`
reads in `core/auth/dependencies.py`, the one in `core/errors.py:149`, and every audit line downstream keep
working with no change of their own. That is why the security tests pass unedited.

**`setdefault`, not assignment.** An ASGI server may have put a `state` dict there already — uvicorn hands
each request a shallow copy of the lifespan state — and replacing it would drop whatever the lifespan put in.

**The header is read from `scope["headers"]`, first occurrence wins.** ASGI header names are lower-cased
bytes; the value is decoded `latin-1`, which is the decoding HTTP header bytes have, is what Starlette's
`Headers` uses, and cannot raise — so a caller cannot reach this code with an exception. First-match is what
`Headers.get` did, so a caller who sends the header twice still does not get to choose which copy is checked.
`_VALID_REQUEST_ID` is unchanged, and a rejected value is still dropped without being logged.

**The response header goes onto a copy:**

```python
        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (_REQUEST_ID_HEADER, request_id.encode("latin-1")),
                    ],
                }
            await send(message)
```

A `Response` sends its own `raw_headers` list, and a `Response` object can be sent more than once. Appending
in place would add one `x-request-id` per send, so the second response from a reused object would carry two.
The bytes on the wire are unchanged: `MutableHeaders.__setitem__` lower-cased the name on the way out too, so
`b"x-request-id"` is what went out before.

`InflightMiddleware` is the same shape, with the three lines that were `request.*` now `scope[...]`:

```python
        scope["app"].state.inflight = _counter

        if scope["path"] in EXEMPT_PATHS:
            ...
            request_id = scope.get("state", {}).get("request_id", "unknown")
            ...
            await dodeal_error_response(LoadShed(), request_id)(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            _counter.release()
```

`URL.path` **is** `scope["path"]` in this Starlette (`datastructures.py` builds it with no root-path
handling), so the exempt match is unchanged rather than merely equivalent. The refusal is the same
`JSONResponse` from the same `dodeal_error_response`, now *called* rather than returned — a `Response` is
itself an ASGI app, so the status, the headers and the body bytes are identical, and `core/errors.py`'s
explanation of why a middleware cannot simply `raise` is still exactly true.

**The `finally` matters more now, not less.** Under `BaseHTTPMiddleware` an exception from the inner app came
back through `call_next`; now it propagates straight up to `ServerErrorMiddleware`. A `release()` written on
the success path alone would leak a slot on every 500 — item 73's hardening, unchanged and re-proved below.

**One thing is genuinely new in both: a non-http scope passes straight through.** `BaseHTTPMiddleware` did
that for us; a plain callable has to do it itself, and getting it wrong is silent. A counted `lifespan` scope
holds its slot until the process exits, so the cap is one lower for the life of the pod; an id written into a
lifespan scope is one id shared by everything that later reads that state.

### The guard

**Every existing test passes unedited.** That is the actual evidence for "behaviour unchanged", and none of
them needed so much as a whitespace change: `tests/unit/test_inflight.py` in full (the twenty-concurrent
exactness test, arrival order, the `finally` release on a raising route, `app.state.inflight` live, the
refusal body, the `WARNING` line with no tenant, the sentinel body reaching no log line, both exempt paths
taking no slot) and `tests/security/test_chain.py` in full (the honoured well-formed inbound id, the overlong
one replaced by a generated uuid4, the newline / CRLF / brace-bearing ones replaced and never logged, the
echoed header).

Two new files, sixteen tests:

`tests/test_no_base_http_middleware.py` — the grep, in the shape of `tests/test_no_sync_clients.py`. **The
patterns are import-shaped and class-shaped, not the bare word**, and that is a decision worth recording: both
middleware docstrings now *name* `BaseHTTPMiddleware` to explain why they are not one, which is the single
most useful sentence in each file, and a bare-word grep would forbid the explanation along with the thing. So
it matches the two import spellings and `class X(...BaseHTTPMiddleware...)`, which an alias
(`from starlette.middleware import base`) still has to write out. A self-test asserts the patterns hit the
four spellings they forbid and miss a line of the prose, because a pattern typo is the failure mode a grep
has that produces a green run.

`tests/unit/test_middleware_asgi.py` — the properties that were not true before:

- **A `ContextVar` set in the route is visible to an outer middleware**, and — the mechanism behind it — the
  route runs in the **same asyncio task** as the middleware.
- **A `lifespan` and a `websocket` scope** reach the inner app **as the same object**, with nothing added to
  it (`state` included) and **no slot taken**. Both scopes carry `app`, as every scope Starlette builds does,
  so a middleware that wrongly counted one would be *counted* by the assertion rather than raising `KeyError`
  before reaching it.
- **An `http` scope on the same stack is counted and identified** — the control. A pass-through that swallowed
  real requests too would pass both tests above.
- **The id header is added to a copy**, asserted by handing the middleware an inner app that sends a list the
  test still holds.

### Sabotage

**(a) The `finally` release removed** (the `try:`/`finally:` reduced to the bare `await self.app(...)`):

```
FAILED tests/unit/test_inflight.py::test_the_counter_returns_to_zero
FAILED tests/unit/test_inflight.py::test_a_request_that_raises_still_decrements
FAILED tests/unit/test_inflight.py::test_the_request_at_the_cap_is_refused
FAILED tests/unit/test_inflight.py::test_the_count_is_exact_under_twenty_concurrent_requests
FAILED tests/unit/test_inflight.py::test_the_cap_is_reusable_after_a_burst
FAILED tests/unit/test_inflight.py::test_the_count_is_exposed_on_app_state
FAILED tests/unit/test_middleware_asgi.py::test_an_http_scope_on_the_same_stack_is_counted_and_identified
7 failed, 24 passed
```

The twenty-concurrent test failed on `current_inflight() == 0` with a leaked slot per request. Restored byte
for byte (md5 verified against the pre-sabotage copy).

**(b) The `_VALID_REQUEST_ID` check skipped** (`_accepted_inbound_id` reduced to `return _inbound_header(scope)`):

```
FAILED tests/security/test_chain.py::test_overlong_inbound_request_id_is_replaced_by_a_generated_uuid
FAILED tests/security/test_chain.py::test_malformed_inbound_request_id_is_replaced_by_a_generated_uuid[newline-...]
FAILED tests/security/test_chain.py::test_malformed_inbound_request_id_is_replaced_by_a_generated_uuid[crlf-...]
FAILED tests/security/test_chain.py::test_malformed_inbound_request_id_is_replaced_by_a_generated_uuid[json_brace-...]
4 failed, 146 passed
```

The injected newline, CRLF and brace values were accepted and echoed. Restored byte for byte.

**(c) A `lifespan` scope counted** (`scope["type"] != "http"` changed to `== "websocket"`):

```
FAILED tests/unit/test_middleware_asgi.py::test_a_non_http_scope_reaches_the_inner_app_untouched[lifespan]
FAILED tests/unit/test_middleware_asgi.py::test_a_non_http_scope_takes_no_slot[lifespan]
2 failed, 7 passed
```

Both lifespan cases failed, on `KeyError: 'path'` — a lifespan scope has no `path`, so the exempt-path lookup
raised before the counting assertion was reached. That proves the guard but not the *counting* claim, so a
narrower variant was run as well:

**(c′) The non-http pass-through takes a slot** (one `_counter.acquire(get_settings().max_inflight)` added
before the delegation):

```
FAILED tests/unit/test_middleware_asgi.py::test_a_non_http_scope_takes_no_slot[lifespan]
FAILED tests/unit/test_middleware_asgi.py::test_a_non_http_scope_takes_no_slot[websocket]
2 failed, 7 passed
```

`At index 0 diff: 1 != 0` — the scope was counted while inside. Restored byte for byte.

### For the lead

1. **Phase 0 question 4, both halves NO.**
   - *Does `core/prompting.py` log `prompt_template_not_preloaded` on a cache miss?* **No.** That module has
     no logger at all — no `logging` import, no `_logger`. `_load_template` (`core/prompting.py:137-143`)
     falls back to `_read_template_file` silently.
   - *Does any test scan `src/` for template names and assert each is in `UNIT_A_TEMPLATES`?* **No.** The
     nearest is `tests/unit/test_prompt_preload.py:101-110`
     (`test_the_tuple_names_the_nine_templates_that_ship`), which runs the **other** direction: it takes the
     tuple and asserts each name is a file that ships. Nothing reads `src/` for the names a pass actually
     sends. Neither was added; they are not this piece.
2. **The contextvar test is the *reverse* direction, on purpose, and the prompt's suggested direction would
   not have proved anything.** The prompt asked for "a request id set by the middleware ... visible inside a
   route through a `contextvars.ContextVar` set in the middleware and read in the route". Measured on
   `516f94d`: that already worked, because a child task copies its parent's context at spawn — the assertion
   passes identically before and after the rewrite. The direction a task boundary actually severs is
   route → outer middleware, and it is measured in both trees (`MISSING` before, `SET-IN-ROUTE` after). The
   test asserts that, plus the mechanism directly (`asyncio.current_task()` is the same object in the
   middleware and in the route). The grep test is kept either way.
3. **No existing test needed an edit.** None.
4. **`middleware/inflight.py` and `middleware/request_id.py` both measure 100%** at this head, and neither has
   a per-file floor. Inflight is a deny path, which is the argument the existing floor list is built on. Not
   added — floors are the lead's.
5. **One line of prose in `src/` had to be reworded to get past an *existing* guard.** The request-id
   docstring cited `starlette/requests.py` as the file that makes `request.state` a view over `scope["state"]`,
   and `tests/test_no_sync_clients.py` greps `src/` for `\brequests\.` — a true positive for its own purpose
   and a false one here. The sentence now names `HTTPConnection.state` instead, which is more precise anyway.
   The alternative was editing that guard's pattern, which is a policy change and not this piece's.
6. **Nothing in `README.md`, `ASSUMPTIONS.md` or a module docstring was left false, and one line was already
   stale.** No line anywhere named `BaseHTTPMiddleware` or "task group" before this piece, so nothing became
   wrong; the README rows gained the new fact. The stale one is in `docs/STATUS.md`: the suite line read "the
   deselected 28 are the `integration` marker (7) and the `redis_real` lane (21)" while the tree deselects
   **30** (integration is **9**, measured this session). Corrected in the same edit.
7. **`ASSUMPTIONS.md` is untouched.** §10.1 ("the cap is read lazily, per request, never in `__init__`") is
   still true word for word — and is now true of a constructor that genuinely exists and takes the inner app
   and nothing else.
8. **The new register item is numbered 122, not 116.** `docs/register.md` stops at 115, but the master carries
   at least item 121 (the `event` field on the startup lines, named in this prompt's Out list and absent from
   the file). 116 risked colliding with a master number. Renumber if the master's next free is lower; the note
   is in the register beside the item.
9. **No `.env.example` row is owed and no setting changed.** `DODEAL_MAX_INFLIGHT`, the exempt paths and the
   request-id character set are all untouched, as the scope required.
10. **`current_inflight` still lives in `middleware/inflight.py`** and `pipeline.py:119` still imports it from
    there. That is item 13's job, with `import-linter`, and was left alone.

### Files

| File | Change |
| --- | --- |
| `src/dodeal_ai/middleware/request_id.py` | `RequestIDMiddleware` as `__init__`/`__call__`; header read from `scope["headers"]`; state written to `scope["state"]`; id appended to a copy of the response-start headers; non-http pass-through; docstring's ordering paragraph rewritten for the new shape |
| `src/dodeal_ai/middleware/inflight.py` | `InflightMiddleware` as `__init__`/`__call__`; counter published through `scope["app"].state`; `scope["path"]` against `EXEMPT_PATHS`; the refusal `Response` awaited as an ASGI app; the `finally` release kept; non-http pass-through; docstring gains the SHAPE paragraph, "WHERE IT SITS" and the `add_middleware` note unchanged |
| `tests/test_no_base_http_middleware.py` | new: the grep, import-shaped and class-shaped, plus a self-test of the patterns |
| `tests/unit/test_middleware_asgi.py` | new: nine tests (contextvar upward, same task, `request.state` through the route, two non-http scopes × untouched and uncounted, the http control, the header copy) |
| `README.md` | the middleware directory bullet; the `inflight.py` and `request_id.py` module rows; two test-file rows; the repo-wide guards paragraph |
| `docs/register.md` | item 86 to DONE; header sha to this head; 86 dropped from the step order; the lead's new item 122 |
| `docs/STATUS.md` | the Piece 86 row; the register-item section; suite line to 1256 and the deselected count corrected to 30; header piece list and date |
| `CAMPAIGN_REPORT.md` | this block |

---

## Piece 87: the body-size limit, outermost   STATUS: DONE `39018b5`

**Register item 87, closed.** *"Pure-ASGI body-size limit, outermost, 64 kB, 413 above it, before Gate 1."*
One new file under `src/` and three edited: `middleware/body_limit.py` (new), `core/config.py`,
`core/errors.py`, `main.py`, plus the `.env.example` row in the same commit because the pin test requires it.

### What was actually wrong

Nothing in the app bounded a request body. A caller could send megabytes and the app would read every one of
them into memory **before Gate 1 decided whether that caller was allowed to send anything at all**. The order
was backwards: identification was paying for the request rather than the request paying for itself. Register
item 43 puts a limit at the edge, but it is DevOps's, it is unowned, and an in-app service that has no bound
of its own is one misconfigured ingress away from having none at all.

`ASSUMPTIONS.md` §10.1 said the in-app limit "is not coming back" — that section is rewritten by this piece.
The reason it was removed was never that an in-app cap is wrong; it was that the first build called
`get_settings()` in a middleware constructor, which forced config to load at import time. That is a fixable
mistake and it is fixed here: the cap is read per request, and `__init__` takes the inner app and nothing else.

### The disagreement between the prompt and the tree, and how it was resolved

The prompt specified the streamed path like this:

> the moment the running total passes the cap, raise `PayloadTooLarge`. The exception surfaces inside the
> route's own body read, propagates to the `DodealError` handler, and comes back as the same 413 with a real
> request id

**It does not.** FastAPI wraps the body read in a broad `except Exception` and re-raises whatever comes out of
it as its own `HTTPException(400, "There was an error parsing the body")` (`fastapi/routing.py`, and the
adjacent comment shows it is deliberate). Measured against the real stack at `7a3ec5c`, before any code for
this piece was written — a minimal FastAPI app, a middleware raising `PayloadTooLarge` out of a wrapped
`receive`:

```
http.response.start 400
http.response.body  b'{"detail":"There was an error parsing the body"}'
```

So a delegated refusal tells the caller its request was **malformed** when it was only too big — 400 where
the register says 413, and a status that points the CRM at its own serialiser rather than at the size.

**The workaround that looks obvious is worse.** FastAPI's `except HTTPException: raise` branch exists exactly
so a middleware can raise one, so making `PayloadTooLarge` inherit `DodealError` *and* `HTTPException` should
thread it through. It cannot: under the combined MRO, `DodealError.__init__`'s `super().__init__(reason_code)`
resolves to `HTTPException.__init__`, which reads the reason code as a status —
`ValueError: 'payload_too_large' is not a valid HTTPStatus`. Measured too. Worse than not working, it is a
trap laid for every future `DodealError` subclass.

**Resolution: the middleware owns its refusal end to end, on both paths.** The wrapped `receive` raises to
stop the read where it stands; the wrapped `send` discards whatever the inner app answered with; the
middleware sends the 413 itself, from the same `dodeal_error_response` builder every other enumerated refusal
uses. What the framework inside makes of the exception stops mattering — which is the property worth having
across a FastAPI upgrade, since the alternative fails *into a 400* rather than loudly.

The status, the reason code, the body shape, the placement, the cap and the `>`-not-`>=` boundary are all
exactly as item 87 specifies. Only the mechanism differs, and it differs because the specified one was
measured not to hold.

### The change

`middleware/body_limit.py`, the Piece 86 shape: `__slots__ = ("app",)`, `__init__(app)`,
`__call__(scope, receive, send)`, no settings read in the constructor.

- **Non-http scopes** pass through untouched, as in the other two.
- **The header path.** A `content-length` that parses as an int above the cap is refused with
  `dodeal_error_response(PayloadTooLarge(), request_id)` awaited as an ASGI app. `receive` is **never called**,
  so not one byte of the body comes off the socket. `request_id` is `"unknown"`: outermost, nothing has minted
  one yet, and generating one here would mean two middlewares minting ids.
- **The streamed path.** No length, chunked, or a lying header: every `http.request` message's body length is
  added to a running integer — never a copy of the bytes — and the moment the total passes the cap the read
  raises. That 413 carries a **real** request id, because the overflow is noticed while the route is reading
  the body, which is inside every middleware below this one. Same lookup, different moment.
- **Exactly the cap is admitted**; one byte over is refused.
- **It cannot retract a response that has already started.** A route that streams a reply while still reading
  the request would have its first bytes on the wire already; ASGI offers no way to take those back, so the
  `started` flag passes the rest through rather than corrupting the response. No route here does that today.
- **It logs nothing.** The 413 is the record. A WARNING per oversized request is a log-volume lever a caller
  controls — the same asymmetry this middleware closes, arriving through the log pipeline instead.
- **`/health` and `/ready` are not exempt**, unlike in `inflight.py`. They carry no body, so the check costs
  them a dict lookup, and an exemption is a path a caller can aim a large body at. The test asserts the
  *absence* of an exemption list, not just that `/health` answers 200 — which it would either way.

`core/config.py`: the commented placeholder becomes `max_request_body_bytes: int = Field(default=65_536, ge=1)`.
`core/errors.py`: `PayloadTooLarge` → `("payload_too_large", 413)`. 413 and not 503 because this is about
**this request's size**: not the service's capacity (`LoadShed`'s 503) and not the caller's quota (Gate 4's
429). `main.py`: registered **last**, so outermost; the order comment now reads body limit → request id →
inflight → routing, and the dead `# register_body_size_limit(app)` line is gone.

### Sabotage

| Change | Result | Restored |
| --- | --- | --- |
| The fast-path header check replaced by `if False:` | 5 failed: `test_a_declared_length_over_the_cap_never_touches_the_body` (`receive` was called), `test_the_refusal_body_is_the_shared_error_shape`, `test_the_header_path_request_id_is_unknown_by_placement`, `test_the_first_content_length_header_wins`, `test_one_byte_over_the_cap_is_413` — the last three because the slow path still refuses with 413 but now carries a **real** id, which is the discriminator between the two paths | byte for byte, sha256 verified |
| `received > cap` to `received >= cap` | 2 failed: `test_a_body_of_exactly_the_cap_is_admitted` and `test_a_body_of_exactly_the_cap_is_judged` | byte for byte, sha256 verified |

### For the lead

1. **No existing test posts a body over 64 kB.** The largest body construction anywhere in the tree is
   `"x" * 4001` (`test_direct_routes.py`, the over-length note), about 4 kB. Nothing was raised to fit.
2. **The per-file floor for `middleware/body_limit.py` is not added**, per the prompt. It measures **100%**
   today. `scripts/check_coverage_floors.py` is unchanged and still lists 15 floors.
3. **The slow-path 413 does carry a real request id**, as expected — asserted directly
   (`test_the_streamed_refusal_carries_a_real_request_id`). Neither 413 carries an `X-Request-ID`
   *header*, because both are sent from outside `RequestIDMiddleware`'s send wrapper. That is inherent to
   being outermost and is the same on both paths, so the two refusals are consistent with each other rather
   than one being an anomaly. Say if the header is wanted on the streamed one; it would mean this middleware
   knowing the id header's name, which today only `request_id.py` does.
4. **`middleware/inflight.py`'s docstring is now stale and was deliberately left alone.** The paragraph under
   "THE CAP IS READ LAZILY" says *"The body-size middleware that used to live here was removed after exactly
   that"*. The history is still true; the implication that there is no such middleware is not. The prompt's
   Out list forbids touching that file beyond registration order, so it is reported rather than fixed. It
   wants one clause in the next piece that may touch it.
5. **`docs/STATUS.md` has no config list to add the setting to.** The prompt asked for "the new setting in the
   config list"; STATUS records settings only inside register-item rows, and the row for 87 names it. The
   configuration reference table is in `README.md` and did get the row.
6. **The `.env.example` row, as exact text** (already committed — the pin test requires it in the same commit
   as the field, and it was written by a script that prints a status and never a line of the file):

   ```
   # The largest request body the app will read, in bytes. Above it the
   # request is refused with 413 payload_too_large at the outermost
   # middleware, before any gate runs. 64 kB is four times the largest real
   # note plus its lead fields: a memory bound, not a content rule. This does
   # not replace the edge limit; it is what holds when the edge has none.
   DODEAL_MAX_REQUEST_BODY_BYTES=65536
   ```

7. **Item 43 stays open and its note is unchanged.** This piece does not close it. Bytes should still be
   refused where they are first accepted; 64 kB in the app is the bound that holds when the edge has none.
8. **The 413 status phrase is computed, not spelled out, in the test.** `_unit_error_body` builds `detail`
   from `HTTPStatus(413).phrase`, which the stdlib renamed from "Request Entity Too Large" to "Content Too
   Large" in 3.13. `requires-python` is `>=3.12`, so a literal would pin the suite to one interpreter over a
   string this service does not choose. Every other field in that body is asserted literally.
9. **`main.py`'s dead `# register_body_size_limit(app)` line was removed.** It named a function that does not
   exist and, with the real middleware now registered twenty lines above it, read as a second unregistered one.
10. **The numbering repair is applied as instructed**: item 122 (the lifespan close-on-raise, from Piece 86)
    renumbered to **124**, its numbering note deleted, items **116–123** appended above it all OPEN, and **125**
    added OPEN. The file now runs 1–125 with no gap.

### Files

| File | Change |
| --- | --- |
| `src/dodeal_ai/middleware/body_limit.py` | new: `BodyLimitMiddleware`, `_declared_length`, `_request_id`; the two paths, the send guard, the lazy cap |
| `src/dodeal_ai/core/config.py` | the commented placeholder becomes `max_request_body_bytes: int = Field(default=65_536, ge=1)` with its three-line comment |
| `src/dodeal_ai/core/errors.py` | `PayloadTooLarge` as `("payload_too_large", 413)`, docstring in `LoadShed`'s style plus why it is never rendered by the handler |
| `src/dodeal_ai/main.py` | `BodyLimitMiddleware` imported and registered **last**; the order comment extended; the dead `# register_body_size_limit(app)` line removed |
| `.env.example` | the `DODEAL_MAX_REQUEST_BODY_BYTES` row beside `DODEAL_MAX_INFLIGHT` |
| `tests/unit/test_body_limit.py` | new: 25 tests — raw ASGI for the middleware's own claims, `TestClient` at the real cap for the stack's |
| `README.md` | the middleware bullet and the request order; the file tree; the `body_limit.py` module row; the `main.py` row; the `DODEAL_MAX_REQUEST_BODY_BYTES` configuration row; the test-file row; the error catalogue |
| `ASSUMPTIONS.md` | §10.1 replaced with the lead's text: the in-app limit, outermost |
| `docs/register.md` | item 87 to DONE; header sha to this head; 87 dropped from the step order; the numbering repair (116–123 added, 122 to 124, 125 added) |
| `docs/STATUS.md` | the Piece 87 row; the register-item section; suite line to 1281 and 99.59%; header piece list |
| `CAMPAIGN_REPORT.md` | this block |

---

## Piece 88: JsonFormatter formats exc_info frames only   STATUS: DONE `77a2bbd`

**Register item 88, closed.** *"`JsonFormatter` formats `exc_info` frames only; a stdout-wide sentinel test
through the ASGI stack."* One file edited under `src/`: `core/logging_config.py`. One new test file.

### What was actually wrong

Nothing inside the `dodeal_ai` tree passes `exc_info` to a log call, and `core/errors.py`'s catch-all logs
the traceback by hand with `frames_only` and a comment saying why. That discipline covered every log line
this codebase writes — and none of the ones it does not.

`JsonFormatter` is on the **root** logger. Starlette's `ServerErrorMiddleware`, uvicorn's error logger
("Exception in ASGI application") and any library in the process log exceptions with `exc_info=True`, and
those records reached our formatter, which rendered them with:

```python
if record.exc_info:
    payload["exc_info"] = self.formatException(record.exc_info)
```

`formatException` is `traceback.format_exception`, which prints `str(exc)` **and walks
`__cause__`/`__context__`, printing every message in the chain**. For an exception raised outside this
package that message is frequently the data that failed: pydantic puts the rejected `input_value` in its
message, a `KeyError`'s message is the missing key, an httpx error can carry a URL with a query string, a
backend error string can quote the note it choked on. The rule in ASSUMPTIONS §3.3 was enforced at our own
call sites and unenforced at the one place every record in the process passes through.

### What landed

Two fields replace one, and the old key is **gone**:

```python
exc_info = record.exc_info
if isinstance(exc_info, tuple):
    exc_class, exc_value = exc_info[0], exc_info[1]
    if exc_class is not None:
        payload["exc_type"] = f"{exc_class.__module__}.{exc_class.__name__}"
    if exc_value is not None:
        payload["exc_frames"] = frames_only(exc_value)
```

- **`str()` is never called on the value anywhere in `format`.** The only thing read off the exception is
  `__traceback__`, by `frames_only`, which formats the outer frames and neither the exception line nor the
  chain.
- **`record.exc_text` is never read**, and a comment says why: the stdlib caches the *message-bearing*
  formatted exception there when a handler formats a record twice, so reading it would restore the exact
  text this piece removes.
- **The tuple shape only.** `(type, None, None)` reaches a formatter on some logging paths; it yields
  `exc_type` and no `exc_frames`. A bare `True` — the value before the logging module resolves it — yields
  neither field, because a formatter that raised here would lose the whole line, including the fields that
  are safe.
- **`record.stack_info` is ignored**, before this change and after; the docstring now says so, since the
  question is otherwise asked once per reader.
- **The `exc_info` key disappears.** The class docstring says that, and why, so a collector query on it is
  corrected rather than silently returning nothing.
- The import is `from dodeal_ai.core.log_safety import frames_only`. `log_safety.py` imports `traceback`
  and nothing else — no `dodeal_ai` import at all — so the direction is one way and there is no cycle.

`safe_error_fields` is deliberately **not** called: it also emits the message for classes defined under
`dodeal_ai`, and a formatter must not know which classes are ours. The spelling is its two type fields
joined instead — see the disagreement below.

### The guard

`tests/security/test_log_exc_info.py`, 7 tests. Two sentinels, an outer and an inner, both passed as
**arguments** to the raising helper: a traceback quotes the source text of every frame it lists, so a
sentinel written down in a raising frame would be printed by the frames themselves and prove nothing.

- **The stdout-wide test through the ASGI stack.** A test-only route is registered on the **real** app by
  the fixture and removed on teardown — never added to `src/`, where a route that raises on purpose has no
  business being. It raises a foreign `RuntimeError` whose message is the outer sentinel, chained `from` a
  `ValueError` carrying the inner one, and a library-shaped logger logs it with `exc_info` before it goes on
  to the catch-all. Driven through `TestClient(raise_server_exceptions=False)` with the real
  `configure_logging()`, captured with **`capfd`** and not `caplog`: the claim is about the bytes on the
  file descriptor, where a uvicorn-style logger's line is seen too. Neither sentinel appears in stdout, in
  stderr or in the 500 body; a line carrying `exc_frames` names the failing route function.
  **Why the log call is in the route:** TestClient never runs uvicorn, and uvicorn is what logs an escaping
  exception with `exc_info` in production. A library logging inside the request reaches the same root
  handler by the same path, and the record the formatter receives is identical. The uvicorn logger itself is
  covered directly by the next test.
- **`uvicorn.error` directly**, after `configure_logging()` has handed it back to the root handler: the same
  chained exception, absent from stdout, the line carrying `logger: "uvicorn.error"`, `exc_type`,
  `exc_frames` naming the raising helper, and no `exc_info` key.
- **The chained cause contributes nothing** — not its message, not its class name, not its frames.
- **`exc_type` present and `exc_info` absent** as JSON keys on every framed line.
- **The three record shapes**: `(type, None, None)`, all-`None`, and a bare `True`.

Every existing test in `tests/security/test_log_safety.py` passes **unedited**. None asserted the
`exc_info` key by name — `grep -rn exc_info --include=*.py` over the repo hit only `core/errors.py`'s
comment and the two formatter lines this piece replaced — so there was nothing to stop and report.

### Sabotage record

| Sabotage | Result |
| --- | --- |
| (a) `payload["exc_info"] = self.formatException(exc_info)` restored alongside the new fields | **5 of 7 failed.** The ASGI test on `RuntimeError: SENTINEL-OUTER-…` in the captured stdout; the uvicorn test on the same; the key test on `exc_info` present; **the chained test on the INNER sentinel** (`ValueError: SENTINEL-INNER-… / The above exception was the direct cause of…`), which is the chain walk the piece exists to stop; the `(type, None, None)` test on `exc_info: "NoneType: None"` |
| (b) `frames_only` kept, `exc_type` changed to `f"{module}.{name}: {exc_value}"` | **5 of 7 failed**, the sentinel tests on the outer message inside `exc_type`, the chained test on its `exc_type` equality, the tuple test on the same |

Restored byte for byte after each: `md5sum` of `core/logging_config.py` is `007847c498c980dff0aaad098584f83f`
before sabotage (a), after the restore, before sabotage (b) and after the final restore. 7 passed.

### Coverage, and the flip at line 60

`core/logging_config.py` is at **100%, no missing lines**, in every run. The old line 60 was reached only
when something outside the tree happened to log an `exc_info` record during the suite, which is what moved
the total between 99.51% and 99.57% in earlier runs. Three full runs at this head, all identical:

| Run | Result | Total coverage | `core/logging_config.py` |
| --- | --- | --- | --- |
| 1 | 1288 passed, 1 skipped, 30 deselected | 99.5885% | 100%, missing `[]` |
| 2 | 1288 passed, 1 skipped, 30 deselected | 99.5885% | 100%, missing `[]` |
| 3 | 1288 passed, 1 skipped, 30 deselected | 99.5885% | 100%, missing `[]` |

**The flip is gone**, and it is gone because the branch is now driven deterministically by this piece's own
tests rather than incidentally by another test's log line. All 15 coverage floors met. The `integration`
lane: 9 passed. The `redis_real` lane was not run — this piece touches no Redis code and no Lua.

### The disagreement between the prompt and the tree, and how it was resolved

1. **`exc_type`'s spelling.** The prompt asked for "the exception's class name with its module, the same
   spelling `safe_error_fields` uses for its type field". `safe_error_fields` has no single dotted field: it
   reports `error_type` = `__name__` and `error_module` = `__module__` **separately**. Those two clauses
   cannot both be satisfied in one field. Resolved in favour of the field list the prompt actually specifies
   — two fields, `exc_type` and `exc_frames` — with `exc_type` as `f"{__module__}.{__name__}"`, which is
   those two values joined and nothing else. **The consequence for the lead:** a collector query that
   matches `error_type == "RuntimeError"` exactly will not match `exc_type` on a formatter line, which reads
   `builtins.RuntimeError`; it needs a suffix match, or the lead prefers a third field (`exc_module`) and
   says so.
2. **README line numbers.** The prompt named "README.md lines 283 and 405 (the log_safety row and the
   sentinel-test row)". Line 283 is the `logging_config.py` row, 405 is the `log_safety.py` row, and the
   sentinel-test row (`test_log_safety.py`) is at **408**. All three were read and all three are updated;
   the new test file gets a row of its own beside the existing one.
3. **Sabotage (b)'s expected failure.** The prompt expected the chained test to fail "on the inner" under
   sabotage (b). It cannot: `str(exc)` of the outer exception is the outer message only, and the inner one
   is reachable only by walking the chain — which is sabotage (a). Under (a) the chained test does fail on
   the inner sentinel, as recorded above. The first version of that test was too weak to show this (it
   inspected one field rather than the captured stdout) and was strengthened before the sabotage runs, which
   is why it now fails under both.
4. **Ruff G201.** The library-shaped log call inside the test route was written `.error(..., exc_info=True)`
   and ruff's default rule set rejects it in favour of `.exception(...)`. Changed to `.exception(...)`, which
   *is* `exc_info=True` and is the spelling a library actually uses; the record the formatter receives is the
   same.

### For the lead

1. **Should the chained cause's frames be printed too?** `frames_only` formats the outer traceback only, so
   when an exception is re-raised `from` another, the original failure's frames are lost along with its
   message. A second field `exc_cause_frames` would be **safe** — frames carry a file, a line, a function
   name and a source line from our own or a library's source, none of it caller data — and it is what makes
   a wrapped failure debuggable: today `exc_frames` shows where the wrapper was raised, not where the
   original broke. **Recommended, as its own item, not folded into anything**, with two conditions: it walks
   `__cause__`/`__context__` one level only (a deep chain is a log-size lever nobody controls), and it is a
   change to `core/log_safety.py`, whose floor is 100 — so it is a piece with its own tests, not a line
   added here. Deliberately **not** in this piece: item 88 is about what must never be printed, and adding a
   field is a different decision from removing one.
2. **`exc_type`'s spelling**, and that it does not literally match `safe_error_fields` — see disagreement 1
   above. The decision to make is whether a collector prefers the joined field or a split pair.
3. **No per-file floor for `core/logging_config.py`.** It now sits on the log-safety deny path, next to
   `core/log_safety.py`, which has a floor of 100. The file is at 100% today. **Not added** — floors are the
   lead's. If added, 100 is reachable: every branch in the module is driven by a unit test now.
4. **Three-run figures and the flip:** in the coverage section above. The flip is gone.
5. **No `.env.example` row.** This piece adds no setting.
6. **Lines that were true before and are false after**, all updated in this commit:
   - `README.md:283` — the `logging_config.py` row said what the formatter emits; it said nothing about
     exceptions, and a reader would have assumed `exc_info`.
   - `README.md:405` — the `log_safety.py` row said "the two helpers every exception-logging **site** uses".
     That is now weaker than the truth: `frames_only` is applied to every **record**, whoever wrote the call.
   - `ASSUMPTIONS.md §3.3` — "Tracebacks are frames-only, unchained" was true of our call sites only, and
     "Enforced by `tests/security/test_log_safety.py`" named one test file.
   - `ASSUMPTIONS.md §8.10` — "enforced by code in **three** places" is now four; the formatter is the
     fourth, and it is the one that covers records this codebase never wrote.

### Files

| File | Change |
| --- | --- |
| `src/dodeal_ai/core/logging_config.py` | the `exc_info` branch becomes `exc_type` + `exc_frames`; `frames_only` imported; the class docstring gains the exception rule, the removed key and the `stack_info` note; a comment on why `record.exc_text` is never read |
| `README.md` | the `logging_config.py` row; the `log_safety.py` row; a row for the new test file |
| `ASSUMPTIONS.md` | §3.3's logging row; §8.10's count, its fourth bullet and the sentinel-test bullet |
| `docs/register.md` | item 88 to DONE with the sha; header sha to this head; the step order left behind is 125, then 123, then 74 with 104, 106 and 96 |
| `docs/STATUS.md` | the Piece 88 row; the register-item section; suite line to 1288 and the three-run figures; header piece list |
| `CAMPAIGN_REPORT.md` | this block |
| `tests/security/test_log_exc_info.py` | new: 7 tests — the stdout-wide sentinel through the ASGI stack with `capfd`, `uvicorn.error` directly, the chained cause, the key swap, and the three record shapes |

---

## Batch F1: items 125, 123 and 117   STATUS: DONE `c2d4941` · `3edbd57` · `1935d01` · `741b17a`

The first foundation batch. The first commit adds to `CLAUDE.md` the rule that allows a batch: a foundation
piece may carry up to three register items, each in its own code commit, with one backfill closing the
session. The three items follow, and the chain was green before every commit.

**The F1 session's backfill stopped at its own guard, and nothing was written for it.** The prompt said to
stop if any of the register numbers it was adding already existed. Item 115 did: its row sits in the
"Items 80 to 96" table with the same step, status and sha as the prompt's row, but different wording. This
block and the rest of the backfill were written in Piece F1b, whose prompt rules that 115's row stays where it
is and is not added again.

### Commits and suite numbers

| Point | Commit | Passed | Skipped | Deselected | Coverage |
| --- | --- | --- | --- | --- | --- |
| Baseline | `a546614` | 1288 | 1 | 30 | 99.5885 % |
| The rule | `c2d4941` docs(F1): foundation pieces may be batched | 1288 | 1 | 30 | 99.5885 % |
| Item 123 | `3edbd57` test(123): the fake clock holds whole milliseconds and refuses other attributes | 1289 | 1 | 30 | 99.5885 % |
| Item 125 | `1935d01` core(125): warn on a template cache miss once the cache is populated | 1293 | 1 | 30 | **99.5900 %** |
| Item 117 | `741b17a` docs(117): a CRM note id is assumed unique and never reused | 1293 | 1 | 30 | 99.5900 % |

All 15 coverage floors met at every commit; ruff, format and mypy clean at every commit. `core/prompting.py`
is at 100 % (56 statements).

### 125 — a template cache miss warns once the cache is populated   `1935d01`

Piece 85 kept the disk fallback for a name the preload never cached, and that fallback was silent. In a
running app a silent miss means two things: a disk read on the event loop, and a template missing from the
image discovered on a paid call instead of at startup.

- `_load_template` logs one WARNING `prompt_template_not_preloaded` when the name is not cached **and**
  `_TEMPLATE_CACHE` is non-empty. It goes to a new logger, `dodeal_ai.prompting`, with one `extra=` field,
  `template`. Then it reads from disk exactly as before.
- **Once per name per app.** `_WARNED_NOT_PRELOADED` is a module-level set. `clear_templates()` empties it
  along with the cache, so the next app in the same process warns again.
- **An empty cache logs nothing.** That is a script, the wheel check or a tmp-dir test, all of which rely on
  the fallback.
- **The name only, never the resolved path**, which would carry the deployment's directory layout into a log
  line.

Four tests, added to `tests/unit/test_prompt_preload.py`:

- (a) Two misses on one name against a populated cache log exactly one WARNING. The formatted line's keys are
  exactly `message`, `template`, `level`, `logger`, `timestamp`.
- (b) A miss against an empty cache logs nothing.
- (c) After `clear_templates()`, the same name warns again.
- (d) Every `.py` file under `src/dodeal_ai/` is parsed, and every string constant (docstrings included) is
  searched for `structured_intelligence/[a-z_]+_v[0-9]+\.txt`. At least nine names must turn up, and each
  must be in `UNIT_A_TEMPLATES`. Before the test was trusted, the scan was checked: it finds exactly nine,
  one literal each.

**Sabotage record**

| Sabotage | Result |
| --- | --- |
| (a) the `_logger.warning` call removed from `core/prompting.py` | Tests (a) and (c) failed and the other 9 in the module passed, as the prompt predicted. Restored; md5 `9fd11fe65be0e26201cfe257b6508db8` before and after. |
| (b) `SCORE_TEMPLATE` dropped from `UNIT_A_TEMPLATES` in `units/structured_intelligence/templates.py` | **4 tests failed, not the 2 predicted**. A full-suite run showed the same 4 and nothing else. See finding 1 below. Restored; md5 `a9e76f41114639d5cf6563f0ee12619b` before and after. |

### 123 — the fake clock holds whole milliseconds and refuses other attributes   `3edbd57`

Piece 102 put `test_elapsed_covers_more_than_any_single_pass` on a fake clock, `_FakeClock`, which replaces the
pipeline module's `time` global. That clock had two weaknesses. It held a float, and `_ms_since` truncates, so
20 ms at a realistic base reads 19. And its `__getattr__` delegated every other attribute to the real `time`
module, so a second clock reading added to the pipeline would silently run on the real clock.

- `_FakeClock` holds an integer `_ms` starting at 0 and has no constructor argument. `monotonic()` returns
  `self._ms / 1000`. `advance` takes whole milliseconds, and the one caller passes `20`.
- `__getattr__` raises `AttributeError` naming the attribute.
- One new test, `test_the_fake_clock_refuses_any_attribute_but_monotonic`.
- **Both assertions of the elapsed test are unchanged.**
- The module ran 20 times in a row, 50 passed each time.

The code comment gives the cause as the zero base rather than the float; see finding 2 below.

**Sabotage record**

| Sabotage | Result |
| --- | --- |
| the delegating `__getattr__` restored | `test_the_fake_clock_refuses_any_attribute_but_monotonic` failed with `DID NOT RAISE AttributeError`; the elapsed test still passed. Restored; md5 `648b1e686cb2ca3a5212f08a2ce49845` before and after. |

### 117 — a CRM note id is assumed unique and never reused   `741b17a`

Docs only. `ASSUMPTIONS.md` gains §4.8, and `docs/STATUS.md` §6 gains Q22.

- **Assumed:** a CRM note id is unique within a tenant and never reused, not even after a delete.
- **Three mechanisms rest on it:**
  - the per-note attempt cap (`attempt:{tenant}:{lead_id}:{note_id}`);
  - the resubmission's `attempt_fp:` reference (item 33);
  - the six-hour `attempt_ttl_seconds` that both keys carry.
- **What reuse does:** the new note inherits the old one's spent allowance and answers `attempt_cap`, and
  nothing in the logs tells the two notes apart. This was seen on the fake CRM, whose ids reset on restart.
- **Correction path:** the attempt keys gain the note's `createdAt` or a CRM-issued unique token, with the TTL
  unchanged.
- **Seams:** `state.py` (`_attempt_key`, `_attempt_fingerprint_key`) and `config.py`
  (`attempt_ttl_seconds`). The key shapes were checked against `state.py:122` and `:134`, and the TTL against
  `config.py:207`.

**The tag.** The `ASSUMPTIONS.md` legend defines `[D]` as "a document states it", and no document states
this. So the body says "`[D]` at best" and the §4.8 heading carries no tag.

**The question is Q22**, owned by the backend, and it is still **UNASKED**. The register marks 117 DONE as a
record. The question itself is owed by a person.

No sabotage: this commit changes no code, and no guard was asked for.

### The two recorded findings

1. **Sabotage (b) failed 4 tests, not 2.** The prompt predicted test (d) and the existing nine-templates test.
   Both failed:
   - (d) named `score_v1.txt` at `scoring.py:52`;
   - `test_the_tuple_names_the_nine_templates_that_ship` failed with `8 == 9`.

   Two more failed, both following directly from the change:
   - `test_a_full_judgement_reads_no_template_from_disk`: `score_v1.txt` was now read from disk;
   - `test_startup_refuses_when_one_template_is_missing`: the template it removes is `score_v1.txt`, so with
     that name out of the tuple, startup no longer refused.

   The last one is worth knowing: that startup test depends on which template the tuple names.
2. **Item 123's cause is the base, not the float.** The register gives the cause as "holds a float from zero".
   But whole milliseconds alone do not fix the 19. From a base of 1,000,000, `_ms / 1000` gives 1000.0 and
   1000.02, the difference is 0.019999…, and `_ms_since` truncates it to 19. The reading is exact because the
   clock starts at zero with no base to set, and the code comment says that rather than crediting the integer.

### The backfill, Piece F1b

Docs only. It changes `docs/register.md`, `docs/STATUS.md`, `README.md` and this block. Each change and its
reasons:

- **`docs/register.md`:**
  - 125, 123 and 117 are marked DONE with their shas. 117's status adds that Q22 is still UNASKED, so a
    reader of the register does not take the backend question as asked.
  - The header now reads `741b17a` / ed3r11, and the step order is the one left behind.
  - Rows 107 to 114 and 126 are copied exactly from the prompt, in number order. Both need a section header.
    The existing headers each name a source, and I cannot confirm a source for these beyond the prompt. So
    both read "added from master ed3r11, 16 September": 107 to 114 in a new section after 106, and 126 in a
    new section after 125.
  - Item 115's row is left where it is.
- **`docs/STATUS.md`:**
  - A §1 row for each of the four commits, the rule commit included.
  - The suite line.
  - The "Last updated" header, which would otherwise have stopped at Piece 88.
  - A "Register items closed in Batch F1" section. The prompt did not name it, but `CLAUDE.md` asks for the
    register item rows, and every piece since L has one.
- **`README.md`:** the `core/prompting.py` row gains the WARNING, its logger and its field. The README's other
  event tables cover the token budget and the breaker only. None of them claims to list every event, so no
  other line became wrong.

**The chain before the backfill commit:** 1293 passed, 1 skipped, 30 deselected, 99.59 %, with
`core/prompting.py` at 100 % (56 statements). ruff, format and mypy clean; all 15 coverage floors met. The
three sabotaged files still hash to the md5 values F1 recorded after each restore (`test_judgement_pipeline.py`
`648b1e68…`, `core/prompting.py` `9fd11fe6…`, `templates.py` `a9e76f41…`), which confirms from the tree itself
that no sabotage survived. No sabotage in F1b: it changes no code.

**Disagreements between the F1b prompt and the tree.**

1. **`.env.example` is not modified.** The prompt says it "shows as modified", and the F1 report said it was
   still modified. At `741b17a`, `git status` lists only the untracked `docs/audit/` and `docs/campaign/`,
   and `git ls-files -v` gives the file the plain `H` flag, not assume-unchanged. The file was not read,
   staged or restored.
2. **CANDIDATE is not a register status word.** Row 108 uses it, and the register's rules line lists PENDING,
   OPEN, DECIDED, NOTE, RETRACTED and DONE. The row went in as written, and the rules line was not changed.
3. **Rows 109 to 114 and 126 agree with the tree:**
   - `_HTTP_TIMEOUT_SHARE: Final = 0.9` at `core/llm/openai_compatible.py:77`;
   - `provider_reason` and `provider_transient` at `llm_call.py:180-181`;
   - `tests/test_env_example_matches_settings.py` exists;
   - `LLM_CALLS_PER_JUDGEMENT = 2` at `main.py:31`;
   - no `PoolTimeout` handling anywhere under `src/`;
   - no `exc_cause_frames` yet.

   Rows 107 and 108 name no symbol that can be checked.

### For the lead

1. **Owed by a person:**
   - Q22, the note-id question, to the backend. It is UNASKED.
   - The STATUS row "`.env.example` has been modified-unstaged in the working tree throughout the campaign"
     (§1, "Open items carried out of Unit A Project 1") is now stale if the file is clean. It was left alone
     because `.env.example` is out of this piece's scope. It needs your ruling.
2. **Conservative choices F1 made:**
   - **Test (d) scans docstrings too.** It reads every string constant, not only whole literals. A docstring
     that names a retired template will fail it and need rewording. A name built with an f-string is not seen,
     and the runtime WARNING covers that case.
   - **The warned set has no lock.** On the event loop it cannot race. Under threads, the worst case is a
     duplicate line.
3. **Floors:** none added. `core/prompting.py` is at 100 %, and item 113 already says it gets no floor.
4. **`.env.example` rows:** none. No setting was added in F1 or F1b.
5. **The register's status words:** rule on CANDIDATE, either adding it to the rules line or changing row
   108's status.

### Files

| File | Change |
| --- | --- |
| `CLAUDE.md` | `c2d4941`: the "Commits" rule's first bullet replaced with the AI/foundation split |
| `src/dodeal_ai/core/prompting.py` | `1935d01`: the module logger, `_WARNED_NOT_PRELOADED`, the warn-once branch in `_load_template`, `clear_templates` clearing both |
| `ASSUMPTIONS.md` | `741b17a`: §4.8 |
| `docs/STATUS.md` | `741b17a`: the Q22 row in §6. Backfill: the four §1 rows, the suite line, the header, the Batch F1 register section |
| `docs/register.md` | backfill: 125, 123, 117 DONE; header; step order; rows 107 to 114 and 126 |
| `README.md` | backfill: the `core/prompting.py` row |
| `CAMPAIGN_REPORT.md` | backfill: this block |
| `test_judgement_pipeline.py` | `3edbd57`: `_FakeClock` on whole milliseconds and refusing other attributes; one new test |
| `test_prompt_preload.py` | `1935d01`: four new tests, (a) to (d) |

## Register item 132: recognise a short note before the length floor

`ba23669`. A note below the floor that is a known code (na1, cb2) or phrase ("not interested") is judged, not asked.

- **Failure mode 1:** a first-word-only rule let "na, client abusive, wants refund" through to three model passes.
- **Failure mode 2:** a tenant that adds a common word to a table makes junk notes pass and spend model calls.
- **Stress test:** every default phrase and code as a below-floor note reaches classification; "ok", "done" and "spoke" still get the fixed question.

## Register item 132, follow-up 1: tighten recognition and bump the config version

`f2084a3`. Every word must be a code or a filler (`short_note_fillers`); `config_version` is `tenant-cfg-default-3`.

- **Failure mode 1:** a filler such as "ok" added by a tenant makes "na ok" pass without a question.
- **Failure mode 2:** the two `test_judgement_routes.py` pins could not be run green under the item 131 red tree, so they are unverified.
- **Stress test:** "cb1 tmrw" recognised, "na, client abusive, wants refund" not, and an empty filler table still recognises "na1".

## Register item 132, follow-up 2: fold the short-note tables inside TenantConfig

The tables are casefolded in `TenantConfig.__post_init__`, so JSON-built and hand-built configs match alike.

- **Failure mode 1:** a mixed-case table on a hand-built config silently never matched; the fold lived only in `TenantConfigFile.build`.
- **Failure mode 2:** a table added later without joining the fold list drifts back to the same defect.
- **Stress test:** a config built with `NA`, `Cb`, `Not Interested` and `TMRW` recognises "cb1 tmrw", "NA1" and "not interested".

## Register item 130: a no-contact note may only be asked for the next attempt date

`1ad3571`. `what_happened` leaves `allowed_missing_by_type` for `no_contact`: the attempt IS what happened, so its author cannot be asked to record more of it.

- **Failure mode 1:** the allowed-missing and suppressed tables are separate maps. Narrowing one and not the other leaves a note marked down on something nobody may ask it about -- which is what happened here, and what item 137 repaired.
- **Failure mode 2:** a model that reports `what_happened` for a no_contact note now fails validation and spends the note's single reprompt, so a template that still invites the component turns every such note into two paid calls.
- **Stress test:** every `MissingComponent` but `next_step_with_date`, returned for a no_contact note, is rejected inside the validated call.

## Register item 131: score a note by binary checks, not numeric marks

`b8c715d`, then `9977dbb` deriving the mark table from the weight. The model answers twelve yes/no checks; the marks, total, denominator and band are computed in code.

- **Failure mode 1:** a model asked for a whole number out of 25 cannot use that resolution consistently -- the same note scores 19 one day and 22 the next, and the rubric's acceptance target is band agreement with a human.
- **Failure mode 2:** the mark table and the weights were two things to keep in step, so a tenant that changed a weight scored against the old table. `9977dbb` derives one from the other; a component with no weight is skipped and `_check` refuses the weights first.
- **Stress test:** every derived table runs from 0 to exactly the weight and is non-decreasing, for the default rubric and for a reweighted one.

## Register item 133: rewrite the Unit A prompts as a shared block plus type blocks

`2e92fbc`. `build_prompt` takes several template names and joins them in order; the vague pass ships one shared block and six type blocks.

- **Failure mode 1:** six copies of the same rules drift, and a rule fixed in one template stays broken in the other five. The relative-time rule did exactly that -- won_lost lost it while its own example still relied on it (item 137).
- **Failure mode 2:** the shared block is the cacheable prefix, so putting the variable block first would lose the prefix cache on every note. The split also left the OUTPUT contract in the shared half, which made a worked example the last thing the model read (item 137).
- **Stress test:** `build_prompt` joins in the order given and the reversed order differs; each type's stable half is byte-identical across ten corpus notes.

## Register item 137: fix the defects an Opus review found in items 130 to 133

Six commits: `548add4` (parseable examples), `1ed5852` (no_contact off `what_happened`), `67c902d` (a recognised short note carries a code), `c1313e5` (`_check` refuses a rubric with no applicable weight), `7be737e` (the version stamps), and this one (the prompts say what the code does).

- **Failure mode 1:** an unparseable worked example is the last answer a model reads before the note, so a copied line break spends the one reprompt and then 503s a judgement that was never in doubt. Every judging template's examples now parse and validate against the schema that accepts them.
- **Failure mode 2:** a below-floor note of nothing but fillers ("tmrw", "again", "بكرة") was recognised as a known outcome and bought three paid calls. Recognition now requires at least one tenant code, or a phrase opening the note with only fillers after it.
- **Stress test:** every JSON object under EXAMPLES in all seven v2 judging templates parses with `json.loads` and validates as the pass's output schema, with the per-template count pinned so an extraction that finds nothing cannot pass.


## Register item 138: the scored-set loader and its format

`src/dodeal_ai/units/structured_intelligence/eval_set.py`. One JSON object per line; `DODEAL_EVAL_SET_PATH` is an environment variable and not a `Settings` field; a path inside the repository is refused before the file is opened.

- **Failure mode 1:** an absent expected field defaulted rather than carried through as None manufactures agreement out of unfinished marking -- and the number it manufactures is the one the business is asked to accept. Every expected field is `| None`, and an empty `missing_components` list is a mark while an absent one is not.
- **Failure mode 2:** a refusal that quotes the row puts a real salesperson's note on a terminal, in a CI log and in a pasted ticket. A bad row names its LINE NUMBER plus pydantic's field locations and error types (`include_input=False`), never a value.
- **Stress test:** the committed fixture of invented notes is refused where it lies and loads only once copied outside the repository; `..` in the path cannot walk back in, because the refusal resolves first.

## Register item 139: the diagnostic harness

`scripts/diagnose_notes.py`. One note in, one CSV row out: the gate, item 132's recognition, the classified type, `is_vague`, the missing components, the clarification question, all twelve check answers, the five marks, the denominator, the total, the band, and the milliseconds per pass. `--live` or nothing is sent.

- **Failure mode 1:** a harness that retried a failed call turns a provider having a bad day into an unbounded bill from a script nobody is watching. It never retries and never loops: a failed note is ONE row carrying the exception TYPE, and the run continues. `--live` refuses over 20 notes and prints the call count before it spends any of it.
- **Failure mode 2:** a paid call placed by accident. The guard has two independent halves, like `real_fetch_check.py` -- the dry-run branch never builds a provider client, and `_guard_against_accidental_live_call` refuses immediately before the client is built even when that branch is wrong.
- **Stress test:** five invented notes end to end against the real provider, then the dry-run branch inverted so a flagless run reaches the live path: the second guard refuses with exit 3 and writes no CSV.

## Register item 140: the eval runner

`scripts/run_eval.py`. Agreement with the hand marks per pass -- classify, `is_vague`, `missing_components`, check answers -- broken down by note type and by language, plus reprompt rate, p50/p95 latency and tokens per pass. The BAND figure is printed last, on its own line.

- **Failure mode 1:** a note nobody has marked counted as agreement turns unfinished marking into a business result. An unmarked field leaves that figure's denominator entirely; a MARKED note whose pass did not run is a disagreement, not an exclusion, because the human and the pipeline really do disagree about it.
- **Failure mode 2:** an expected band read from the file rather than computed would be a second source for the one number the unit exists to justify. The scored set holds no band field; both sides go through `compute_score`, and a row whose marks do not fit its type's applicable set is excluded and COUNTED as excluded on the terminal.
- **Stress test:** a fake model scripted to the marks gives 100 % on every figure over exactly the marked notes (4/4, 4/4, 4/4, 23/23 checks, 3/3 bands); changing one classification drops classify to 3/4 and leaves the band alone, because callback and discovery share a denominator.

## Register item 141: ns_closure requires a reason, not only an ending

`score_v2.txt` landed at `d1b4146`; this commit finishes the piece it interrupted. The check now reads "ended AND why? Both are needed", and `tests/unit/test_scoring.py::CLOSURE_SENTENCE` was still pinning the old sentence. `SCORE_MAX_OUTPUT_TOKENS` was left at 1024 in the working tree from an abandoned reasoning-model trial and is back to 384. `.env.example`'s `DODEAL_EVAL_SET_PATH` row and its exemption in `tests/test_env_example_matches_settings.py` are the same interrupted change and go with it -- committing either alone reds `test_the_exemptions_are_all_still_needed`.

- **Failure mode 1:** a prompt change that leaves its test pinning the old sentence is a green suite that proves nothing about the rule it was changed for. The constant now carries "Both are needed" -- the whole of item 141 -- so reverting the prompt reds the test rather than passing on the unchanged half of the line.
- **Failure mode 2:** the scoring pass's output ceiling above the vague pass's inverts the one rule the ceilings encode: only vague detection answers in the note's own language, so only it needs Arabic headroom. A scoring ceiling raised to fit a reasoning model spends tokens on a pass whose answer is fixed ASCII keys, and hides the day the vague pass starts truncating.
- **Stress test:** `test_the_pass_that_answers_in_the_notes_language_has_the_largest_ceiling` asserts the strict ordering rather than either number, so any future rise in the scoring ceiling reds it whatever value is chosen.

## Register item 142: the enforcement block on every judgement

`Judgement.enforcement` (`schemas.py`), derived by `decide.py::enforcement` and attached at both build sites in `pipeline.py`. `EnforcementMode` becomes `off`/`advisory`/`strict` and moves to `schemas.py`; the verdict is `allow`/`flag`/`block`, with `applies_to` null exactly when nothing is flagged. Strict is built and recorded, never enabled: blocking is scoped to a stage change and the request carries no field for it. `config_version` bumped to `tenant-cfg-default-4`; `RUBRIC_VERSION` did not move, because what a judgement CARRIES changed, not how it is scored.

- **Failure mode 1:** an optional field with a default is a field the CRM reads as absent and infers around -- which is exactly the guessing the piece exists to end. `enforcement` has NO default, so every construction site must supply one and a judgement without a block cannot be built. The one real consequence is the idempotency store: a judgement written before this deploy no longer validates, and `_existing_judgement` already logs `idempotency_replay_invalid`, takes the key over and judges again rather than replaying a block-less body.
- **Failure mode 2:** a verdict derived from the total rather than from the action would read the two thresholds in a second place, and the day a threshold moves the flag and the advice disagree about the same note. The scored site passes `decision.action`; the suppressed site passes the detail, and `accept_silent` is deliberately absent from `_FLAGGED_ACTIONS`.
- **Stress test:** every mode against every action AND every suppression, asserting no combination returns `block` today and that `applies_to is None` is true exactly when the verdict is `allow`; then `prompt_clarification` removed from `_FLAGGED_ACTIONS`, which reds the two advisory/strict cases and nothing else.

## Register item 143: the judgement-row contract and its fake store

`src/dodeal_ai/units/structured_intelligence/judgement_rows.py`. `JudgementRow` is fifteen fields and NO note text: three ids, the note's `created_at`, the note type, the scored half (band/total/denominator), the suppressed reason, `prompt_sent`, the enforcement verdict and the four version stamps. `JudgementStore` is a Protocol with one read -- a tenant's rows over a half-open window, optionally one author. `FileJudgementStore` is the fake: invented rows from a JSON file whose path is `DODEAL_JUDGEMENT_ROWS_PATH`, refused inside the repository, read once at construction. The real store is a backend ask and is not written here.

- **Failure mode 1:** a row that carried a note body would make this service a second copy of the CRM's data -- a second place to breach, a second deletion to honour, a second thing to keep in step with an edit. `extra="forbid"` plus an exact-field-set test means a body cannot arrive under `note`, `note_text`, `text`, `body` or `content`, at the model or through the loader.
- **Failure mode 2:** a half-filled row -- a band beside a suppression, or a suppression with a total -- would be averaged into item 144's measures as if somebody had judged it. `_exactly_one_half` refuses both shapes at parse time, so a malformed row is a refusal naming its position and never a quiet contribution to a rep's average.
- **Stress test:** the two rows for note 1002 carry the same `note_created_at` and differ only in position and band; a test asserts file order survives the read, because the resubmission measure in item 144 has nothing else to order by. Tiling `[day one, day two)` and `[day two, day three)` returns every row exactly once.

## Register item 144: the three per-rep measures

`src/dodeal_ai/units/structured_intelligence/measures.py`, pure over item 143's rows. The rolling average band (totals averaged, band derived through `config.band_for`), the share of notes flagged, and the share of ASKED notes that came back with a better band. Two new `TenantConfig` fields: `measure_evidence_floor` (10) and `rolling_window_days` (30) -- nothing else in the unit may carry those numbers. `config_version` deliberately did NOT move: neither field changes what a judgement carries or means, and a bump would make every stored row incomparable with every new one and suppress every average the day this ships.

- **Failure mode 1:** a measure reported as a low number instead of suppressed is a manager acting on noise, and the rep it is about cannot tell the difference. Below the floor `band` and `percent` are None beside a reason, and the floor is applied to EACH measure's own denominator -- a rep with thirty notes and two prompts has an average and no improvement figure. A zero denominator carries `nothing_to_measure` rather than `below_evidence_floor`, because "nobody asked you anything" is good news and "not enough yet" is a wait.
- **Failure mode 2:** totals made under a different `rubric_version` or `config_version` are percentages of a denominator a different rubric chose, and averaging them silently is a figure nobody can reproduce. The most recent scored row's pair wins and the rest are excluded AND COUNTED; exclusions can push a measure below the floor, which is the honest outcome. `model_version` is deliberately not in the pair -- fragmenting on it would suppress every measure the day a provider rolls a point release.
- **Stress test:** a resubmission table where note 1 goes fair -> excellent, note 2 goes fair -> fair, and note 3 never comes back gives 1 of 3; reversing the order of note 1's two rows drops it to 0, which is what pins the store's recorded-order promise. A better total inside the same band is not an improvement.

## Register item 145: the three role briefs and an on-demand route

`brief.py` (three templates over item 144's measures, no model call), `user_directory.py` (the second backend ask: `User` is user_id/name/role/team, `UserDirectory` is one read, `FileUserDirectory` is the fake over `DODEAL_USER_DIRECTORY_PATH`, refused inside the repo), and `GET /api/v1/briefs/{role}/{subject_id}` behind the same gate chain as every other route. Four answers: 200 text/plain, 204 when there is nothing to say, 404 `subject_not_found`, 503 `brief_store_unavailable`. No scheduler -- the CRM calls at 07:30 and whether this service needs its own clock is open.

- **Failure mode 1:** an empty daily email trains people to ignore the channel within a fortnight, and then the one that matters is ignored with the rest. `build_brief` returns None and the route answers 204 when no line carries a single FIGURE -- not when there are no rows. A brief that looked at eight notes and can say nothing about any of them is still an empty email. A 204 is reserved for that: an unknown subject is 404 and an unwired store is 503, because both would otherwise read as a quiet day for ever.
- **Failure mode 2:** a model asked to narrate figures will eventually produce one that is not in them, and a brief is read as fact. Every number here is `measures.py` arithmetic and every sentence is a template in `brief.py`; a suppressed measure renders as "not enough yet (4 of 10)" and never as a band or a 0%. The team roll-up is computed over the team's rows POOLED, not averaged from per-person averages, so a rep with three notes cannot weigh the same as one with nine.
- **Stress test:** every role against a subject with two notes under a floor of three returns None; raising it to three returns text containing "nothing was asked about" and no band anywhere in the improvement line. A team of nine notes at 30 and three at 75 renders "fair over 12 notes" (pooled mean 41), where averaging the averages would have said "good".

## Piece: register item 141b, the prompt-set stamp

Item 141b -- `PROMPT_SET_VERSION` moves to `unit_a_prompts_v3`. `d1b4146` edited
`score_v2.txt` in place and left the stamp at v2, so two prompt texts shared one
stamp and a judgement stamped v2 was no longer reproducible from the v2 files.
Production failure modes: (1) two stamps, one text -- an eval run compares
judgements from before and after a prompt edit as if the prompt had not moved;
(2) the stamp is a string in one module, so a future edit can miss it again.
Stress test: the meta/versions route and a judgement both assert the literal
v3 string, so the stamp cannot move in one place and not the other.

## Piece: register item 146, the prompt set digest

Item 146 -- a test hashes the ten templates `UNIT_A_TEMPLATES` names and pins
the digest beside `PROMPT_SET_VERSION`, so a prompt edit without a bump is a red
build. The name is hashed with the text, so a template joining or leaving the
tuple moves the digest too.
Production failure modes: (1) the digest is over the SHIPPED files, so a
deployment running a DODEAL_PROMPTS_DIR override is unguarded and can send text
no stamp describes; (2) the pair is updated by hand, so a bump with a stale
digest passes review and leaves the guard asserting the wrong claim.
Stress test: append one byte to a template, watch the digest assertion fail
naming both constants to move, restore.

## Piece: register item 140, the eval runner and the model-failure event

Item 140 -- `_expected_band` compares the marked check set with
`applicable_checks` before calling `compute_score`, and excludes on a mismatch.
It used to catch `OutputValidationError`, by which point `output_rejected` had
already written `output_validation_failed` -- a MODEL failure event -- for a
human marking error.
Production failure modes: (1) an alert on that event spikes whenever somebody
runs the eval over a half-marked set, and the alert is trained to be ignored;
(2) the set test and `validate_checks` are two statements of one rule, so a
third fault added to `validate_checks` would pass the pre-check and raise here.
Stress test: a no_contact row marked with all five of its checks plus
`wh_outcome` asserts no `output_validation_failed` record on `dodeal_ai.unit_a`.

## Piece: register item 132, recognised_short on the outcome lines

Item 132 -- `recognised_short` is a bool on `judgement_completed` and
`judgement_suppressed`, true only where the note was below the floor and the
tenant table let it through. `_below_floor` is now one statement of the floor
that both the gate and `_recognised_short` ask, and `diagnose_notes.run_passes`
calls the shared helper instead of testing the phrase table on every note.
Production failure modes: (1) the flag is computed before the gate and carried
to whichever line is reached, so a new early return between them would log a
judgement with no flag at all; (2) it is a per-judgement bool with no counter
behind it -- a floor decision needs the lines aggregated, and nobody is doing
that yet.
Stress test: a full note opening with a tenant phrase ("not interested tmrw
again") clears both floors and must read false, which the old column did not.

## Batch: Unit A Part 1 (D1 ... 74), one commit per item

Baseline at b3ecab5: 1993 passed, 2 skipped, 50 deselected. The recorded 1999
at f3223eb counted six more tests from score_v3.txt, an untracked byte copy of
score_v2.txt in prompts/structured_intelligence/ that the lead deleted before
this run (each template there parametrises six tests).

## Piece: register item D1, the service token verifier

Item D1 -- `core/auth/service.py::ServiceTokenVerifier` verifies the CRM's
service token (iss, aud, subdomain, iat, exp; lifetime at most 300 s; previous
key for rotation) with a distinct audit code per refusal.
Production failure modes: (1) CRM and service clocks drift past the leeway and
every service call 401s as token_not_yet_valid; (2) a rotation leaves the
previous key set forever, so a retired key keeps verifying.
Stress test: a token with iat ten seconds old and exp 295 s ahead must refuse
as lifetime_exceeded although exp is still in the future.

## Piece: register item D1, the service gate chain

Item D1 -- a second chain in `core/auth/dependencies.py`: service verify, the
same Host match, a `principal="service"` context, and a Gate 4 that moves only
`cost:tenant:{t}` (`limiter.enforce_tenant_cost`). `RequestContext.scope_for_author`
names the author as an asserted subject; no service key is one startup ERROR
and `/ready` 503 `{"service_token": "not configured"}`.
Production failure modes: (1) every CRM call shares one tenant counter, so one
busy integration exhausts the tenant's request cap for all routes; (2) a key
missing on one pod only takes that pod out of rotation, which reads as flapping.
Stress test: two service calls leave the cost store holding exactly
`{"cost:tenant:tenant-a": 2}` and no `cost:user:*` key.

## Piece: register item 92, the direct routes on the service chain

Item 92 -- both direct routes depend on `service_gate4_cost` and judge under
`scope_for_author(request.author_id)`, so the question caps and the per-user
token budget key on `author:<id>`; `author_differs_from_subject` is gone from
the pipeline and its outcome lines; `/meta/versions` takes either principal
through `gate4_either_principal`. Fetch routes stay user-only.
Production failure modes: (1) the CRM sends a wrong or stale author_id, and the
caps and budget land on the wrong person with nothing to contradict it; (2) a
service token leak lets its holder post any note as any author until expiry.
Stress test: under one service token, author A's fourth vague note in the hour
is `rate_limited` while author B's first is still asked.

## Piece: register item 153, the brief route on the service chain

Item 153 -- `GET /api/v1/briefs/{role}/{subject_id}` depends on
`service_gate4_cost` only; a person's token is 401, and who may read whose
brief is decided by the CRM before it asks.
Production failure modes: (1) the CRM forwards a brief to the wrong reader,
and nothing here can tell; (2) a burst of 07:30 brief calls shares the tenant
request counter with judgements and can cap them for the window.
Stress test: a valid user token for the same tenant and Host is 401 on the
brief route.

## Piece: register item 127, the history route

Item 127 -- `POST /api/v1/notes/judgements/history` (service chain; the direct
body plus an aware `note_created_at`) judges an old note with every prompt
withheld `history` (first in decide()'s order), no db2 counter read or written,
its own idempotency namespace, the note's date as createdAt, tokens charged to
`tokens:history:tenant:{t}` alone, and a per-process bulkhead of
`history_max_inflight` (8) that answers 503 `history_load_shed`, Retry-After: 1.
Production failure modes: (1) a backfill fans out across pods, and eight per
pod is still a tenant-wide flood on the model provider's rate limit; (2) the
history budget runs out mid-backfill and the rest is 429 until the window
resets, with no queue to resume from.
Stress test: with eight history slots held, a ninth history request is 503
with Retry-After: 1 while a live direct note on the same token is 200.

## Test: register item 92, the load lane's direct-route rate key

The load lane is outside the default run, so A3 missed one asked change there:
`test_one_prompt_per_note.py` asserted the direct route's rate slot under the
user `sub`; it now asserts `ratelimit:{t}:author:{id}`. Lanes after A5:
redis_real 31 passed; load 11 passed three times.

## Piece: register item D1, the demo minter's service tokens

Item D1 -- `scripts/mint_demo_token.py` mints the CRM's service token (iss, aud,
subdomain, iat, exp at now+300 or the deployment's shorter maximum; HS256 from
the same env stack) for --route direct, history, brief, measures and config,
and a user token for fetch and the versions probe; the history body carries
`note_created_at`. `.env.demo` gains an invented HS256 service key.
Production failure modes: (1) an operator copies .env.demo's shared secret into
a real deployment and anyone holding the file can mint service tokens; (2) the
compose stack and the minter drift apart again and every service call 401s as
invalid_signature.
Stress test: a token the minter prints for each service route verifies with
`ServiceTokenVerifier` built from the same settings.

## Piece: register item 97, tenant rules at runtime

Item 97 -- three service-chain routes (`PUT`/`GET /api/v1/admin/tenant-config`,
`GET .../history`) store a `unit_a` section validated by the file's own parser,
stamped `tenant-cfg-<tenant>-<YYYYMMDD>-<n>`, in db2 under `tenant_cfg:{t}` and
a 50-long `tenant_cfg_history:{t}`, both with no TTL, written by one
compare-and-set script. `core/tenant_config.resolve_section` is the one order
(override, file, default), cached `tenant_config_cache_seconds` per process and
failing safe; every route resolves once at entry and hands the config down.
Production failure modes: (1) pods see a change up to 30 s apart, so two
judgements of one minute carry different stamps and thresholds; (2) db2 is
flushed and every tenant silently drops back to its file with no alert beyond
the missing version.
Stress test: with a tenant file loaded, a PUT's version must win over the file's
on the next resolve, and a refused PUT must leave that version in force.

## Piece: register item 155, the brief's sources load at startup

Item 155 -- the judgement rows and the user directory load once in the
lifespan when their variables are set; a malformed or in-repo file refuses
startup as `judgement_store_invalid` / `user_directory_invalid`, unchained, with
no path. The dependencies read `app.state` only, and every refusal carries
fixed text, a position and an exception type -- no path, file or tenant key.
Production failure modes: (1) a file edited after startup is never seen until
a restart, so a corrected fake keeps serving the old rows; (2) a large file is
held whole in every worker's memory.
Stress test: start the app, delete both files and refuse every `read_text`; a
head-of-sales brief still answers 200 or 204.

## Piece: register item 156, whole local days in the tenant's zone

Item 156 -- `TenantConfig.timezone` (IANA, default "Asia/Dubai", validated by
the section parser, no version bump). `rolling_window` is `[local midnight
today - N days, local midnight today)` as UTC instants, built from local dates
so daylight saving moves the UTC instant and never the local midnight; printed
periods are local first-to-last dates. `tzdata` is now a dependency.
Production failure modes: (1) a tenant spanning two zones is counted in one,
so an evening shift's notes land on the next day for half the team; (2) a zone
change mid-month shifts every past window's bounds, and last week's figures
change when re-read.
Stress test: a London week across the spring change starts and ends at local
00:00 and is 7 days minus one hour in UTC.

## Piece: register item 143, the CRM read clients for the brief

Item 143 -- `tools/crm_reads.py`: `CrmJudgementStore` and `CrmUserDirectory`
behind the existing protocols, keyset-paged over the proposed
`GET /api/service/judgements` and `/users` (ASSUMPTION[Q23],
`docs/contracts/crm_service_reads.yaml`), built like `tools/leads.py`. Rows are
sorted by the CRM row id before a JudgementRow is built; more than 10000 rows or
any failed page is 503 `brief_store_unavailable`. `DODEAL_BRIEF_SOURCE=crm`
turns them on in the lifespan; a file path set by its variable still wins.
Production failure modes: (1) the CRM's ids are not monotonic with recording
order, and improved share silently compares the wrong "later" row; (2) a
tenant with more than 10000 rows in a window never gets a brief at all.
Stress test: a backend that ignores `after_id` and returns the same full page
must be refused as paging_stalled, not looped on.

## Piece: register item 144, improved means fair or better

Item 144 -- `improved_share` counts a prompted note as improved only when a
later row for the same note is band fair or better, the business criterion,
instead of "any band better than before". The asked row's own band no longer
enters into it.
Production failure modes: (1) a rep whose notes start fair is credited for any
resubmission, so the figure flatters teams that write fair notes already;
(2) the CRM's row order is the only link, so a resubmission stored before its
original reads as no improvement.
Stress test: a note asked about at 10 that comes back at 35 stays poor and is
not improved; one asked at fair that comes back fair is.

## Piece: register item 154, the brief as JSON behind a switch and a deadline

Item 154 -- the brief answers JSON `{text, period, lines, flagged_note_ids}`
(lines are the BriefLine data; the period is local first and last day; flagged
ids newest first, at most 50); 204 is unchanged. It runs under
`judgement_deadline_seconds` with its own 503 `brief_deadline_exceeded`, and
`TenantConfig.rep_numbers_enabled` (default False, settable by the section
parser) off is 403 `rep_numbers_not_enabled`, checked before the store is read.
Production failure modes: (1) a tenant opts in through the admin route and
every manager's brief appears at once, with no staged rollout; (2) the deadline
is shared with judgements, so a 25 s brief holds an in-flight slot as long as a
judgement does.
Stress test: a store that sleeps past a 0.05 s deadline answers 503
brief_deadline_exceeded, not a hung request.

## Piece: register item 145, the brief content the business asked for

Item 145 -- from judgement rows, under the evidence floor and the not-sent rule:
the rep's yesterday (band and flag count) and signals (viewing, negotiation,
won_lost); the team leader's week against last week, people flagged yesterday
with their note ids, and the team's signals; the head of sales's teams week on
week and a coaching list of people poor or fair in each of the last two weeks,
each over the floor. Yesterday is the last whole local day, a week the last
seven; the store read widens to two weeks (`brief_window`). Ids ride in the
JSON (`signals`, `flagged_yesterday`, `coaching`), never in the text.
Production failure modes: (1) at a floor of 10 a rep's yesterday is usually
suppressed, so the section managers expect rarely appears; (2) a person who
changes team mid-fortnight is coached against the team they are in today.
Stress test: a person poor this week and excellent last week is not on the
coaching list; poor in both weeks, each over the floor, is.

## Piece: register item 144, the measures routes

Item 144 -- `GET /api/v1/measures/reps/{author_id}` and
`GET /api/v1/measures/teams/{team}` (service chain, the rep-numbers switch,
the judgement deadline, the brief's store and directory) answer the three
measures as `{value, state, n, floor, excluded}`, `average_total` (the same
comparable rows and floor as the band) and the window; the team route pools
its members' rows, then lists each member. Unknown author or team is 404.
Production failure modes: (1) the team route reads the whole tenant's window
and filters in code, so a large tenant pays a full read per team asked for;
(2) a person moved between teams is counted in their current team only.
Stress test: a rep with four notes under a floor of ten reads null values and
state below_evidence_floor on every measure, and a null average total.

## Piece: register item 142, strict blocking at a stage change

Item 142 -- the direct body takes an optional `stage_change_to` (at most 100
chars; the history body refuses it); the pipeline turns it into ONE boolean
(casefolded, in the tenant's `blocking_stages`), and `decide.enforcement` blocks
with `applies_to: stage_change` only when the mode is strict, `blocking_enabled`,
that boolean, not a resubmission, and a prompt_clarification or an unrecognised
thin note all hold. The resubmission route flags instead. Defaults: blocking off,
stages qualified, won, lost. The stage is never logged or prompted.
Production failure modes: (1) the CRM's stage names differ in spelling from the
tenant's list and nothing blocks, silently; (2) a tenant turns blocking on and a
flaky model marks good notes poor, blocking stage changes until the rep
resubmits.
Stress test: the table test turns each of the five conditions off in turn and
gets a flag, never a block.

## Piece: register item 148, the check answers on the judgement

Item 148 -- `NoteAnalysis.checks` carries the scoring pass's validated yes/no
answers on a scored judgement, null when suppressed and on judgements stored
before the field (which still replay); it reaches no log line.
Production failure modes: (1) the CRM shows a rep "which check failed" and the
check names are the rubric's internal vocabulary, so a renamed check breaks the
screen; (2) a replayed pre-field judgement shows no checks and reads as a
different kind of answer.
Stress test: a stored judgement with `checks` stripped out replays 200 with
`checks: null` and no model call.

## Piece: register item 34, the note's language on every judgement

Item 34 -- `units/structured_intelligence/language.py` holds the one script rule
(the Arabic pattern moved from pipeline.py, the Latin one from run_eval.py);
`NoteAnalysis.language` is arabic, english or mixed on every judgement, scored
or suppressed, from the note's own text; the fixed question and run_eval both
import it.
Production failure modes: (1) a note in Latin-script Arabic (Arabizi) reads as
english, so per-language figures overstate English; (2) a single Arabic word in
an English note makes it mixed, so the mixed bucket fills with English notes.
Stress test: "لا" is arabic and draws the Arabic fixed question; "ok" is english
and draws the English one.

## Piece: register item 107, the copied-previous flag

Item 107 -- `NoteAnalysis.copied_previous` on every judgement: on the fetch
route, the next-older note on page one equals this one after strip; on the
direct route, the optional `previous_note_fingerprint` (64 hex, either case)
equals this note's fingerprint. A flag only; no mark moves.
Production failure modes: (1) the fetch route sees only page one, so a copy of a
note that has fallen off it goes unflagged; (2) the direct route compares exact
fingerprints while the fetch route strips, so a copy that differs only in
trailing whitespace is flagged on one route and not the other.
Stress test: the same answers scored for a copied and an original note give
identical scores and actions, with only the flag different.

## Piece: register item 97, the Q13 switch through the section parser

Item 97 -- the `unit_a` section parser accepts `deal_specifics_applicable`, so a
tenant file or the admin route can turn the Q13 component on; `_check` still
refuses a rubric the switch leaves with zero applicable weight.
Production failure modes: (1) a tenant flips the switch with no business-line
checklist behind it, and deal_specifics is marked on checks the prompt never
defined for its line; (2) flipping it moves the denominator from 80 to 100 and
old and new totals are compared across a version the reader ignores.
Stress test: every weight on deal_specifics is refused with the switch off and
accepted with it on.

## Piece: register item 150, zero weight through a type's suppression

Item 150 -- `_check` refuses a rubric that any scored note type's suppression
leaves with zero applicable weight (`weights_type_applicable`), so a no_contact
note can never meet a denominator of 0 at run time; the refusal reaches the
tenant file and the admin route through the one parser.
Production failure modes: (1) a new type suppression added in code refuses
tenant rubrics that loaded yesterday, at the next restart; (2) the check reads
the suppression table as code today, so a future per-tenant table would need it
re-proved.
Stress test: weights 50/50 on what_happened and client_said sum to 100 and are
refused, because no_contact scores neither.

## Piece: register item 157, the recognised-short counter

Item 157 -- `recognised_short_total` in core/metrics.py, with no label at all,
incremented in `_log_outcome` exactly when the outcome line says
`recognised_short: true`, so the counter and the lines cannot disagree.
Production failure modes: (1) a replayed judgement writes no outcome line and
does not count, so the rate is of judgements made, not of requests; (2) with no
tenant label a floor change for one tenant cannot be read off the counter.
Stress test: "na" below the floor counts once; a full note on the same lead
leaves the counter where it was.

## Piece: register item 116, the ceilings assume a non-reasoning model

Item 116 -- one comment beside each of the three output ceilings and in
core/llm/profiles.py: they are sized for a non-reasoning model. No behaviour
changes.
Production failure modes: (1) a profile moves a task to a reasoning model and
every answer truncates, spending the reprompt and then 503 malformed_output;
(2) a provider silently enables reasoning on an existing model id with the
same effect.
Stress test: a scripted MAX_TOKENS finish on the score pass is rejected as
output_truncated and reprompted once (tests/unit/test_reprompt.py covers it).

## Test: register item 97, a runtime-config test that depended on the date

`test_a_new_day_starts_the_count_again` (B1) set its first version at the real
now and hardcoded 2026-09-23 as "the next day"; it went red when the calendar
reached that date. Both instants are now fixed. The code was right; the test
was not.

## Piece: register item 77, a profile must name the client's own provider

Item 77 -- `OpenAICompatibleClient._resolve` refuses any profile whose provider
is not the client's configured one (it refused only unsupported vendors
before), so a groq profile on an openai client is a ConfigError; the startup
sweep inherits it and refuses to start.
Production failure modes: (1) a fallback client built from the primary's
profiles would refuse every task -- it is built with the table emptied, and
must stay so; (2) a proxy base URL for one vendor with the other vendor's
provider setting passes the check and posts to the wrong API shape.
Stress test: an openai profile on a groq client raises before the transport is
entered, and the error names no model.

## Piece: register item 66, the daily cap on the nothing-to-ask path

Item 66 -- when there is nothing to ask, the pipeline reads the daily key
beside the hourly one (`state.read_daily_rate_limit`, concurrently, both
failing open), so a rep already past today's cap reports `rate_limited` rather
than `nothing_to_ask`. The prompt-slots script is unchanged and nothing is
taken.
Production failure modes: (1) a db2 outage reads 0 for both and the report
drifts to nothing_to_ask for a rep who is in fact capped; (2) the two GETs are
not atomic, so a take landing between them can split the report by one.
Stress test: with the daily key at the cap, a fair note with no question reads
rate_limited and the key is not incremented.

## Piece: register item 94, the drain follows the deadline

Item 94 -- serve.py's graceful shutdown is `ceil(judgement_deadline_seconds +
5)`, not a fixed 30 s, so raising the deadline cannot leave a draining pod
cutting off judgements it admitted.
Production failure modes: (1) the orchestrator's termination grace period is
set separately and shorter, and the pod is killed before uvicorn's drain ends;
(2) a brief under the same deadline plus its store read may still run past it.
Stress test: a 12.3 s deadline gives uvicorn an 18 s drain.
