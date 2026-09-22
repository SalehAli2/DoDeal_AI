# CLAUDE.md: rules for every Claude Code session in this repository

A prompt carries only what varies: Title, Phase 0, Scope, Ordered build,
"For the lead". Everything else is here. Where a prompt and this file
disagree, this file wins unless the prompt says "overrides CLAUDE.md" and
names the rule.

The master document (held by the lead, outside this repo) is the position.
The code is the fact: where the two disagree, the code wins and the
document gets a defect to record. Register item numbers come from the
master and from `docs/register.md`, which lags the master; if a prompt
names an item you cannot find in either, say so in the report and carry on.
## Phase 0, before any code

1. `git status`; `git log --oneline -5`. The prompt names the expected head
   sha. If the head differs, stop and report.
2. Read the files the prompt names, `src/` first, then docs.
3. Write a ten-line state-back: what each named file does today and the one
   thing the piece changes in it. Then build.
## The stopping chain

Green before every commit, no exceptions:

```
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```


- Run `uv run ruff format .` right after writing code.
- `uv run python scripts/check_coverage_floors.py` must pass. Floors are
  never changed unasked; a change is a policy decision for the lead.
- The default run is hermetic with or without a local Redis. Red only when
  Redis is up is a suite defect to report, never a reason to stop Redis.
- No test has a re-run exemption. Anything red in an unattended run: stop,
  leave the tree, record BLOCKED with what failed. Never fix forward.
- `redis_real` lane: `uv run pytest -m redis_real --no-cov` with
  `DODEAL_REDIS_REAL_URL` on a database the service does not use (db 9).
  A hand run; it skips, never fails, without a server. Run it when the
  piece touches Redis or Lua. `integration` lane: `-m integration --no-cov`.

## Commits

- AI pieces carry one register item per session (prompts, classify, vague,
  score, rubric, decide, the provider adapter, redaction, evaluation,
  brief narration). Foundation pieces carry up to five (Redis, middleware,
  logging, config, CI, test infrastructure, deployment, docs). One code
  commit per item. Unclear which kind: treat it as AI, stop after one.
- Stage by explicit path. Never `git add .` or `git add -A`.
- Never amend, never force-push, never `reset --hard`. Do not push unless
  the prompt says to. No `Co-Authored-By` trailer, whatever else is said.
- Message: `type(item): one line`. Types: `core`, `unit-a`, `tools`,
  `test`, `docs`, `ci`.

## Code rules

- Comments say what and why in at most three lines. Every setting has a
  three-line comment: what it is, why this default, what a wrong value
  does. Test docstrings are one sentence. Longer reasoning goes to the
  report or `ASSUMPTIONS.md`.
- Fail-open or fail-closed is decided per dependency by what its failure
  risks. Never unified. Auth and tenancy fail closed; the cost gate fails
  open; the idempotency reservation fails closed; the confirm, rate limit
  and attempt counters fail open.
- Every Redis call runs inside its connection's breaker and catches
  `RedisError`, never a subclass.
- `core/redis.py` carries no numeric literal (tested). Timeouts and sizes
  come from `Settings`.
- Model output is untrusted text until `llm_call.parse_output` validates
  it. The total, the band and the decision are computed in code.
- A hardening claim is verified by sabotage: revert the one line, watch the
  named tests fail, restore byte for byte, and say which tests failed.
- Where the prompt does not decide something, take the conservative option,
  do it, and record it under "For the lead".
- Never make a test agree with the code when the code is wrong. If a test
  fails because the behaviour changed in a way nobody asked for, stop,
  report it under "For the lead", and leave the test red. Changing an
  assertion to match new behaviour is only correct when the prompt asked
  for that behaviour.

## Never

- Invent a backend endpoint or field. The CRM surface is exactly
  `GET /leads`, `GET /leads/{id}`, `GET /leads/{id}/notes` under
  `/api/service`, with `DD-API-KEY`. Treat no backend behaviour as proven.
- Accept note text in a request body on the primary route. The direct route
  is the one recorded exception.
- Accept a band or a total from any input. Loosen `LeadNote`. Re-enable
  PyJWT's `sub` check.
- Raise the global external timeout to fit a model call. Retry a paid call.
  Feed a rejected model answer back into a prompt (`with_tail` takes a
  template name, never a string).
- Log note text, model output, the clarification prompt, a Redis key
  containing a fingerprint, a rejected input value, or a foreign
  exception's message. Frames and types only.
- Put a real tenant name, a real note, or anything from the real export in
  a fixture, a prompt or a test.
- Store anything under the idempotency key but the validated judgement.
- Apply a register item outside its carrying step. If the scope is wrong,
  say so; do not widen it.
- Patch `get_cost_client` or `get_operational_client` with anything but the
  shared fakes in `tests/helpers/`.
- Use a sync HTTP or Redis client, `time.sleep`, or `urllib.request` under
  `src/` (tested).

## Tests and docs the piece owes

- Floors kept, none added unasked. A new module that needs one goes under
  "For the lead".
- Docs only when the prompt says docs. By default the lead writes them at
  the end of the day.
- Test files are named in the report and run by the chain. They are never
  on the "where to review" list.
- Every piece appends a block to `CAMPAIGN_REPORT.md` in its code commit:
  the item number and name, the two production failure modes, and the one
  stress test from the explainer. Three or four lines. This is the only doc
  a piece writes by default, because the explainer is terminal output and
  is lost when the session ends.
## Redis, when a piece needs it

The lead does not operate Redis. Check `docker compose ps`, start it if
needed, wait for PONG. If Docker is not running, stop and say "Open Docker
Desktop, then rerun." Set `DODEAL_REDIS_REAL_URL=redis://localhost:6379/9`
and `DODEAL_REDIS_REAL_REQUIRED=1` inside every lane command. Leave Redis
running. Repeat a lane run three times only for a new concurrency test.

## Final message, at most 30 lines, in this order

1. The sha per commit, one line each.
2. One suite line: passed, skipped, deselected, coverage, floors.
3. One lane line, if a lane ran.
4. One sabotage line per item: what was changed, which tests failed,
   restored.
5. Disagreements between the prompt and the tree, and how each was
   resolved.
6. For the lead: conservative choices; `.env.example` rows as exact text;
   anything owed by a person; anything the prompt asked for that is not
   there.
7. Where to review: `src/` files and functions only, one line each on what
   to verify that the diff cannot show. Never a test file.
8. The push command, not run.

Then the explainer block below, at most 12 lines.

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