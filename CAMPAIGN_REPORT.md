# Campaign report — Unit A Project 1

**Started:** 1 Sep 2026
**Branch:** `scaffold/core-governance-homes`
**Base head:** `36a0210` — refactor(workers): delete Celery, add the arq skeleton (D2, part 3 of 3)

Resume mechanism: a session may stop at any phase boundary. The next session runs Phase 0,
reads this file, resumes at the first phase not `DONE`, and never redoes a `DONE` phase.

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

## Phase A — unit schemas and the TenantConfig seam   STATUS: DONE <sha>

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

## Phase B — db2 operational client and the three state concerns

**STATUS: NOT STARTED**

## Phase C — judgement routes, TenantScope, DodealError, SEAM[STEP3]

**STATUS: NOT STARTED**

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
