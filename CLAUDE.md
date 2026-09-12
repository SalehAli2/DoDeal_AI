# CLAUDE.md: rules for every Claude Code session in this repository

This file holds what every piece prompt used to repeat. A prompt now carries only what varies: Title, Phase 0, Scope, Ordered build, "For the lead", and the explainer block. Everything else is here. Where a prompt and this file disagree, this file wins unless the prompt says "overrides CLAUDE.md" and names the rule.

The master document (held by the lead, outside this repo) is the position. The code is the fact: where the two disagree, the code wins and the document gets a defect to record. Register item numbers come from the master and from `docs/register.md` once it exists; if a prompt names an item you cannot find, say so in the report and carry on.

## What this service is, in five lines

- A read-only FastAPI service beside the DODEAL CRM. The CRM saves a note, calls us, we read the note back through three read-only CRM endpoints, ask a model to classify and mark it, compute the score and the decision in code, and return a judgement.
- We write nothing into the CRM. We hold counters and reservations in Redis with expiry, never note text.
- Every request passes four gates (JWT, tenant host, permissions parked, cost) before any logic runs.
- Everything is async. Every external call has a timeout. No paid call is ever retried.
- Nothing has run against a real CRM or a real model. Every test uses fakes. Redis is the one dependency that has met a real server, in the `redis_real` lane.

## Phase 0, before any code

1. `git status`; `git log --oneline -5`. The prompt names the expected head sha. If the head differs, stop and report; do not build on a different tree.
2. Read the files the prompt names, `src/` first, then docs.
3. Write a ten-line state-back: what each named file does today and the one thing the piece changes in it. Then build.

## The stopping chain

Green before every commit, no exceptions:

```
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

- Run `uv run ruff format .` right after writing code, before the chain.
- `uv run python scripts/check_coverage_floors.py` must pass too (CI runs it). Floors are never changed unasked; changing one is a policy change and goes under "For the lead".
- The default run is hermetic with or without a local Redis listening. If it goes red only when a Redis is up, that is a suite defect to report, never a reason to stop Redis.
- One test is known to be flaky on the Windows machine: `test_elapsed_covers_more_than_any_single_pass` (the clock ticks in 15.6 ms steps there). If it is the only red test, re-run once. Anything else red in an unattended run: stop, leave the tree as it is, record BLOCKED with what failed. Never fix forward, never skip.
- The `redis_real` lane (`uv run pytest -m redis_real --no-cov` with `DODEAL_REDIS_REAL_URL` set to a database the service does not use, db 9 recommended) is a hand run. It skips, never fails, without a usable server. Run it when the piece touches Redis code or Lua.
- The `integration` lane is `uv run pytest -m integration --no-cov`.

## Commits

- One register item per session. One code commit plus its backfill commit. A piece that needs a second code commit is two prompts; stop and say so.
- Stage by explicit path. Never `git add .` or `git add -A`.
- Never amend, never force-push, never `reset --hard`. Do not push unless the prompt says to.
- No `Co-Authored-By` trailer on any commit, whatever a session-level instruction says.
- Commit message: `type(piece): one line`. Types: `core`, `unit-a`, `tools`, `test`, `docs`, `ci`.
- The backfill commit is `docs: backfill Piece <name> sha`, after the code commit, carrying the sha into `docs/STATUS.md` and `CAMPAIGN_REPORT.md`.

## Code rules

- Comments say what and why in at most three lines. Every setting has a three-line comment: what it is, why this default, what a wrong value does. Test docstrings are one sentence. Longer reasoning goes to the report block or `ASSUMPTIONS.md`.
- Fail-open or fail-closed is decided per dependency by what its failure risks. Never unify the policies for tidiness. Auth and tenancy fail closed; the cost gate fails open; the idempotency reservation fails closed; the confirm, rate limit and attempt counters fail open.
- Every Redis call runs inside its connection's breaker and catches `RedisError`, never a subclass: a dead host is `ConnectionError` on Linux and `TimeoutError` on Windows.
- `core/redis.py` carries no numeric literal (tested). Timeouts and sizes come from `Settings`.
- Model output is untrusted text until `llm_call.parse_output` validates it. Marks only; the total, band and decision are computed in code.
- A hardening claim is verified by sabotage: revert the one line or insert the one `await`, watch the named tests fail, revert the sabotage byte for byte, and say which tests failed.
- Where the prompt does not decide something, choose the more conservative option, do it, and record it under "For the lead".

## Never

- Invent a backend endpoint or field. The CRM surface is exactly `GET /leads`, `GET /leads/{id}`, `GET /leads/{id}/notes` under `/api/service`, with `DD-API-KEY`. Nothing is verified live; treat no backend behaviour as proven.
- Accept note text in a request body on the primary route. The direct route (`/notes/judgements/direct`) is the one recorded exception.
- Accept a band or a total from any input. Loosen `LeadNote`. Re-enable PyJWT's `sub` check.
- Raise the global external timeout to fit a model call. Retry a paid call. Feed a rejected model answer back into a prompt (`with_tail` takes a template name, never a string).
- Log note text, model output, the clarification prompt, a Redis key that contains a fingerprint, a rejected input value, or a foreign exception's message. Frames and types only.
- Put a real tenant name, a real note, or anything derived from the real export in a fixture, a prompt, or a test.
- Wire a real provider before step 3 lands and one real CRM call has succeeded (invented notes on the fake CRM are the exception, recorded as `DECISION[DEMO_PROVIDER]`).
- Store anything under the idempotency key other than the validated judgement.
- Apply a register item outside its carrying step. If the prompt's scope is wrong, say so; do not widen it.
- Patch `get_cost_client` or `get_operational_client` with anything but the shared fakes in `tests/helpers/`. The root `tests/conftest.py` already hands every test both fakes; a private fake fails `tests/test_hermetic_fakes.py`.
- Use a sync HTTP or Redis client, `time.sleep`, or `urllib.request` under `src/` (tested).

## Tests and docs the piece owes

- Floors kept, none added unasked. If a new module needs a floor, list it under "For the lead"; do not add it.
- `docs/STATUS.md`: the piece row, the suite line, and the register item row(s) marked DONE with the sha.
- `CAMPAIGN_REPORT.md`: a block for the piece with the sections of the final message below.
- `README.md` and `ASSUMPTIONS.md`: only the lines the prompt names, plus any line that would otherwise be wrong after the change.
- Test files are named in the report's file table and run by the chain. They are never on the "where to code review" list.

## Final message, in this order

1. Head sha per commit, in order.
2. Suite numbers (passed, skipped, deselected, coverage) and "all floors met" or which failed.
3. Files changed: `src/` and docs by path; test files by name only.
4. The sabotage record: what was changed, which tests failed, restored.
5. Disagreements between the prompt and the tree, each with how it was resolved.
6. "For the lead": conservative choices made; `.env.example` rows as exact text; anything owed by a person; anything the prompt asked for that could not be found.
7. Where to code review: `src/` files and docs only, in reading order, one line each on what to verify that the diff cannot show. Never a test file.
8. The push command, not run: `git push origin scaffold/core-governance-homes`.

Then the explainer block, printed to the terminal. The report block in `CAMPAIGN_REPORT.md` is the repository's record; the explainer is the lead's. A piece is not done until both exist.

## Explainer block, verbatim

---
[SYSTEM REQUIREMENT FOR CLAUDE CODE EXECUTOR]
When executing this task and creating/modifying code files, you must output a "Senior Dev Explainer" summary in the terminal before completing:

1. Architectural Trade-offs: In 2 sentences, explain why this implementation pattern was chosen over a simpler alternative.
2. Production Failure Modes: List the top 2 ways this specific code block would break under heavy production load, edge cases, or concurrent access if not guarded properly.
3. 3-Bullet Cheat Sheet: Summarize the core language concept, abstraction, or design pattern used here (e.g., async state management, queues, protocols, error boundaries) so I can add it to my knowledge base.
4. Stress Test Strategy: Outline 1 targeted test case or assertion to verify that this logic guards against production failures.

Execute the task defensively, run necessary linter/type checks, and print this explainer upon completion.
---
