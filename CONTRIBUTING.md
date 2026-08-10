# Contributing

## Run locally

1. `uv sync`
2. Create `.env` with at least `DODEAL_JWT_SIGNING_KEY=<any-local-value>` — the
   app fails closed (refuses to start; `/ready` returns 503) if this isn't set.
   **The file must be saved as UTF-8 with no BOM** — a BOM breaks env-var
   parsing silently.
3. Start Redis: `docker compose up -d`
4. Run the app: `uv run uvicorn dodeal_ai.main:app --reload`
5. Run tests: `uv run pytest`

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
- **Test after every change**, not just before a PR.
- **Tests must stay hermetic.** No live Redis, network, or LLM calls in the
  suite — mock/fake them (see `tests/unit/test_cost.py`'s `FakeRedis` for the
  pattern). If a test needs real infrastructure, it doesn't belong here.

## Decisions not to reverse

These are deliberate, not oversights. See `ASSUMPTIONS.md` for the full,
current list and status of every provisional decision.

- **Gate 3 (permissions) is parked**, not wired into the live chain — the
  token carries no roles yet.
- **PyJWT's `verify_sub` is disabled.** The token's `sub` claim is an integer;
  PyJWT requires a string. Don't re-enable it.
- **The cost gate fails open on Redis outage.** Intentional — see above.
- **The broad `except Exception` in `core/errors.py`'s catch-all is
  intentional.** It's the outermost fail-closed boundary; don't narrow it.

## Commit style

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
`docs:`, `chore:`, `style:`, `refactor:`, `test:` — optionally scoped, e.g.
`feat(cost): ...`, matching existing history.
