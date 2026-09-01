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

## Phase C — judgement routes, TenantScope, DodealError, SEAM[STEP3]   STATUS: DONE <sha>

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

## Phase D

**STATUS: NOT STARTED**

## Phase E

**STATUS: NOT STARTED**

## Phase F

**STATUS: NOT STARTED**

## Phase G

**STATUS: NOT STARTED**

## Phase H

**STATUS: NOT STARTED**

## Phase I

**STATUS: NOT STARTED**

## Phase J

**STATUS: NOT STARTED**

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
