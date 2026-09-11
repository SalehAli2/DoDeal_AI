# Contributing

## Run locally

1. `uv sync`
2. `uv run pre-commit install` — the hooks run ruff check, ruff format and
   mypy on every commit. Without them, CI is the first place a formatting
   failure shows up.
3. Create `.env` with at least `DODEAL_JWT_SIGNING_KEY=<any-local-value>` — the
   app fails closed (refuses to start; `/ready` returns 503) if this isn't set.
   **The file must be saved as UTF-8 with no BOM** — a BOM breaks env-var
   parsing silently.
4. Start Redis: `docker compose up -d`
5. Run the app: `uv run uvicorn dodeal_ai.main:app --reload`
6. Run tests: `uv run pytest`

**Using Claude Code on this repo?** The permission rules travel with the tree:
`.claude/settings.json` is committed, so every session starts with the same
allow / ask / deny list — `git push` always asks, and `--amend`, `--force`,
`reset --hard`, `rm -rf` and reading `.env` are denied outright. Do not put
personal or machine-specific rules there. They belong in
`.claude/settings.local.json`, which `.gitignore` excludes (along with anything
matching `.claude/*.local.*`) and which Claude Code merges over the shared file.

## Architectural rules (not enforced by tooling)

- **Single config source.** Every setting is read through
  `core/config.py::get_settings()`. Never hardcode a config value or read an
  env var directly elsewhere.
- **Unknowns are isolated behind a seam and tracked in `ASSUMPTIONS.md`.** If
  you're guessing at an external contract (a claim name, a response shape, a
  role), don't scatter the guess — put it behind a swappable seam and log it
  in `ASSUMPTIONS.md` with how to correct it once confirmed.
- **Clients get generic errors; the real reason goes only to the audit/error
  log.** Never let an exception message, a claim value, or a stack trace reach
  an HTTP response body.
- **Fail-closed vs. fail-open is deliberate, not an inconsistency to "fix."**
  Auth and Tenancy fail closed (an outage risks a data breach). The cost gate
  fails open (an outage risks a bounded, recoverable, logged spend). Don't
  unify these.
- **Everything the service needs at runtime lives inside `src/dodeal_ai/` and
  ships in the wheel.** `scripts/verify_wheel.py` runs in CI and fails if an
  installed copy cannot import every module or find its prompts.
- **Untrusted strings that become identifiers (tenant, host, request id) are
  validated against a fixed pattern at the boundary, once, and lowercased.**
  Downstream code never re-validates and never sees the raw value.
- **Never log `%r` or `str()` of a foreign exception or any payload.** Use
  `core/log_safety.safe_error_fields` and `frames_only`. A foreign exception's
  message is frequently the data that failed (pydantic puts the rejected value
  in it); our own exceptions keep fixed-vocabulary messages, so keep it that way
  when you add one. A sentinel test in `tests/security/test_log_safety.py` fails
  if raw content reaches a log line.
- **Sentinel tests raise through a helper that binds the value on a non-raising
  line, or the assertion matches its own source text.** A sentinel test asserts
  some secret string never reaches the log. If the `raise` statement itself
  contains that string, the traceback frame quotes the source line, the string
  appears in the captured output, and the test fails on its own text rather than
  on a real leak — or worse, is "fixed" by weakening the assertion. Bind the
  sentinel on an earlier line and raise the name.
- Run all four checks before every push, and read every result:
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`. On Windows PowerShell 5 `&&` does not work; use
  `uv run pytest; if ($?) { uv run ruff check . }; if ($?) { uv run ruff format --check . }; if ($?) { uv run mypy }`
  so the chain stops at the first failure. CI runs two more on top of these
  four — `scripts/check_coverage_floors.py` and the wheel build plus
  `scripts/verify_wheel.py`. Run them locally too before a push that touches
  packaging or a module with a coverage floor.
- **Tests must stay hermetic.** No live Redis, network, or LLM calls in the
  suite — mock/fake them (see the shared fakes in `tests/helpers/` for the
  pattern). If a test needs real infrastructure, it doesn't belong here.
- Model calls go through `core/llm/` only. No provider SDK is imported
  anywhere else, and the `LLMClient` Protocol stays one method — routing,
  fallback, breakers and quota grow behind `get_llm_client()`, never onto
  the interface (`docs/FUTURE_PATTERNS.md` item 3). Adapters receive an
  `AssembledPrompt` and pass it through unchanged: they read `.stable` and
  `.variable` and never re-split `.text`, or the injection boundary moves
  out of `core/prompting.py`.
- Raw note text and model output are never logged. `AssembledPrompt.variable`
  and `LLMResponse.text` are excluded from repr for this reason; don't
  reintroduce them into a log line by another route.

## Decisions not to reverse

These are deliberate, not oversights. See `ASSUMPTIONS.md` for the full,
current list and status of every provisional decision.

- **Gate 3 (permissions) is parked**, not wired into the live chain — the
  token carries no roles yet.
- **PyJWT's `verify_sub` is disabled.** The token's `sub` claim is an integer;
  PyJWT requires a string. Don't re-enable it.
- **JWT leeway is 30s by default.** Never set it to `0` in production — clock
  drift between the CRM and this service then 401s valid users.
- **The cost gate fails open on Redis outage.** Intentional — see above.
- **The broad `except Exception` in `core/errors.py`'s catch-all is
  intentional.** It's the outermost fail-closed boundary; don't narrow it.
- LLM calls use `retry=False` and pass `timeout=llm_timeout_seconds` per
  call. A timed-out model call may have completed and billed.
  `llm_timeout_seconds` (60s) is deliberately separate from
  `external_call_timeout_seconds` (10s, sized for a CRM fetch) — never raise
  the global to fit a model call.
- `llm_model` has no real default and the factory refuses an empty value.
  A pinned model id is set per deployment, never drifted by a default.
- **Backend keys are per tenant and have no default.** An unknown tenant fails
  closed before any network call; never fall back to another tenant's key.
- **Per-file coverage floors are the real gate for security modules; the 92%
  total is a floor, not a target.** The repo-wide number is an average, so a
  gate module can rot to 60% while well-covered code elsewhere keeps the total
  green. `scripts/check_coverage_floors.py` sets a minimum per *file* for
  `core/auth/**`, `core/tenancy.py`, `core/cost/**`, `core/errors.py`,
  `core/validation.py` and `core/log_safety.py`. Never lower a floor to make a
  build pass — write the test.

## Branch protection (repository settings, not code)

**This is configured by the repository owner in GitHub settings. Nothing in this
tree enforces it, and nothing in this tree can.** A clean checkout with a green
local run tells you nothing about whether these rules are actually on — check
the repository settings.

`main` and `scaffold/*` require, before any merge:

- **A green status for every required CI check**, which is all six in
  `.github/workflows/ci.yml`: `ruff check`, `ruff format --check`, `mypy`,
  `pytest` (total coverage floor 92%), `scripts/check_coverage_floors.py` (the
  per-file floors), and the wheel build plus `scripts/verify_wheel.py`. All six
  are *required*, not merely reported — a check that is advisory is a check that
  gets ignored on a Friday.
- **No force-push.** History on these branches is append-only. This is the same
  rule the working process already follows (never amend, never force-push); the
  branch setting is what makes it true for everyone rather than a habit.
- **No merge on red.** No admin override, no "it's only the coverage floor", no
  merging a PR whose checks are still running.

## Security checkpoint per feature

Before any new feature or tool ships, ask which of the
[OWASP LLM Top 10](https://genai.owasp.org/llm-top-10/) apply to THIS feature
and confirm each is handled. A full pass happens before the pilot (Phase 4).
The foundation already covers some structurally: the cost gate (unbounded
consumption), the validation layer (improper output handling), the
server-side prompt builder (injection), tenant isolation + safe logging
(sensitive-info disclosure), and clean prompts with no secrets (system-prompt
leakage). See `docs/FUTURE_PATTERNS.md` item 8 for the full checklist and
when to run it.

## Future patterns — read at the right phase

`docs/FUTURE_PATTERNS.md` collects patterns worth adopting later, each tagged
with WHEN it becomes relevant so nothing is built too early or forgotten.
Trigger comments in the code (e.g. `core/resilience.py`, `core/prompting.py`)
point back to the matching item at the seam where that work will happen.

## Commit style

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
`docs:`, `chore:`, `style:`, `refactor:`, `test:` — optionally scoped, e.g.
`feat(cost): ...`, matching existing history.
