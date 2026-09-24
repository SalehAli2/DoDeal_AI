# CLAUDE.md: rules for every Claude Code session in this repository

A prompt carries only what varies: Title, Phase 0, Scope, Ordered build,
"For the lead". Everything else is here. Where a prompt and this file
disagree, this file wins unless the prompt says "overrides CLAUDE.md" and
names the rule.

The master document (held by the lead, outside this repo) is the position.
The code is the fact: where the two disagree, the code wins and the
document gets a defect to record. If a prompt names a register item you
cannot find, say so in the report and carry on.

## Lean by default

Full discipline only where a mistake costs money, leaks data or judges a
person. These are the RISK AREAS:

- authentication, tokens and tenant isolation;
- paid calls, budgets, retries and idempotency;
- model-output validation, including the quote check;
- scoring, and anything that judges a person;
- personal data: masking, logging, retention.

Everything else (docs, scripts, demo tools, config plumbing, floors,
refactors) is low risk: build it, test its behaviour, move on.

## Phase 0, before any code

1. `git status`; `git log --oneline -5`. If the head is not the one the
   prompt names (or a descendant it allows), stop and report.
2. Read this file. Then read JUST IN TIME: only the files the current item
   touches, when you reach it. Search first (`Select-String`, grep), then
   read only the part you need. Never re-read a file already read.
3. State-back in at most five lines, then build.

## The checks

Per commit:

```
uv run pytest -q --no-header <tests for the touched modules> <guard tests>
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

- The guard tests always run: hermetic fakes, `.env.example` pinning, the
  prompt-set digests, the assumption markers.
- Run `uv run ruff format .` right after writing code.
- Once at the end of the session, before the last commit: the full
  `uv run pytest -q --no-header` and
  `uv run python scripts/check_coverage_floors.py`.
- If that final run is red because of an earlier commit in this session,
  fix it in one extra commit and report it. If the cause is unclear, stop,
  leave the tree, and report. Never hide a red.
- The default run is hermetic with or without a local Redis.
- `redis_real` lane (`uv run pytest -m redis_real --no-cov`, db 9): only
  when the session changed Redis or Lua code, once at the end. `load`
  lane: only for capacity work. `integration` lane: only when HTTP
  transport code changed. A lane repeats three times only for a new
  concurrency test.

## Commits

- One commit per risk-area item. Low-risk items may share one commit.
- Stage by explicit path. Never `git add .` or `git add -A`.
- Never amend, never force-push, never `reset --hard`. Push only when the
  prompt says so. No `Co-Authored-By` trailer.
- Message: `type(item): one line`. Types: `core`, `unit-a`, `unit-b`,
  `tools`, `test`, `docs`, `ci`.

## Code rules

- Comments say what and why in at most three lines. Every setting has a
  three-line comment: what it is, why this default, what a wrong value
  does.
- Fail-open or fail-closed is decided per dependency by what its failure
  risks. Never unified.
- Every Redis call runs inside its connection's breaker and catches
  `RedisError`. `core/redis.py` carries no numeric literal.
- Model output is untrusted until validated. Scores, bands, decisions and
  verdicts are computed in code, never taken from a model.
- SABOTAGE only for guards in a risk area: revert the one line, watch the
  named tests fail, restore byte for byte, say which failed. None for
  low-risk items.
- Where the prompt does not decide something, take the conservative
  option, do it, and record it under "For the lead".
- Never make a test agree with wrong code. If behaviour changed in a way
  nobody asked for, stop and report. When the prompt asked for the change,
  update the EXACT expected values; never loosen an exact assertion into a
  partial one.

## Never

- Invent a backend endpoint or field. The CRM surface is `GET /leads`,
  `GET /leads/{id}`, `GET /leads/{id}/notes` under `/api/service` with
  `DD-API-KEY`, plus the two PROPOSED reads in
  `docs/contracts/crm_service_reads.yaml`. Treat no backend behaviour as
  proven.
- Accept a band, a total, a mark, a verdict or a version stamp from any
  input.
- Retry a paid call, with ONE recorded exception by the lead: a
  transcription or model pass whose response never arrived may be retried
  once. A response we received is never paid for again. No library may
  retry a paid call on its own; switch library retries off.
- Feed a rejected model answer back into a prompt.
- Log note text, transcripts, model output, summaries, prompts, audio
  URLs, signed links, signatures, secrets, phone hashes, voiceprints, a
  key containing a fingerprint, a rejected input value, or a foreign
  exception's message. Ids, counts, fixed codes, types and frames only.
- Put a real tenant, note, call or anything from a real export in a
  fixture, prompt or test.
- Read or edit `.env` or any file holding real values. Sessions may write
  `.env.example` and `.env.demo`, placeholders only.
- Edit a released prompt file (`*_v1.txt` and the like). A prompt change
  is a new file under a new stamp, with the digest re-pinned.
- Use a sync HTTP or Redis client, `time.sleep`, or `urllib.request`
  under `src/`.

## Tests and docs

- Write tests for behaviour, not for coverage. The overall gate (92 %)
  covers low-risk code.
- Coverage floors of 100 only for risk-area modules, added when the prompt
  says. Floors are never lowered unasked.
- Test files are named in the report, never on the "where to review" list.
- Docs only when the prompt asks.
- One `CAMPAIGN_REPORT.md` block per session, at most ten lines: the items,
  each risk-area item's two failure modes, and one stress test.

## Redis, when needed

The lead does not operate Redis. Check `docker compose ps`; start it with
`docker compose up -d redis` if needed and wait for PONG. If Docker is not
running, stop and say "Open Docker Desktop, then rerun." The real-lane
variables are set in the session environment. Leave Redis running.

## Final message, at most 20 lines, in this order

1. The sha per commit, one line each.
2. One suite line: passed, skipped, deselected, coverage, floors.
3. One lane line, if a lane ran.
4. One sabotage line per risk-area item.
5. Where the tree differed from the prompt, and how it was resolved.
6. For the lead: conservative choices; anything owed by a person;
   anything asked for that is not there.
7. Where to review: `src/` files and functions only, one line each.
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
