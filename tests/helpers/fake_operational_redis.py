"""FakeOperationalRedis — the in-memory stand-in for the db2 connection.

Lives here, not inside one test module, because the state tests, the route
tests (Phase C) and the pipeline tests all need it. Injected the way the cost
tests inject theirs:

    monkeypatch.setattr(state, "get_operational_client", lambda: fake)

Implements exactly the four commands units/structured_intelligence/state.py
issues -- SET (with NX/XX/EX), GET, DELETE and the prompt-slots EVAL -- and no
more. A command the unit does not use is a command whose fake semantics nobody
has checked against real Redis.

EVAL mirrors the prompt-slots script and the idempotency take-over script
against the same dict and records each call in `evals` as (script, keys,
outcome); the real scripts run in test_cost_lua.py. The outcome codes are
imported from state.py, never retyped.

NO CLOCK. `ex` and the script's windows record a TTL in `ttls` and nothing ever
counts it down, so a test asserting "the window was set to 3600" reads
`fake.ttls[key]` rather than waiting. Expiry is therefore simulated by deleting
a key, and a key with no expiry (the audit-M4 edge, TTL == -1) is simulated by
a `store` entry with no `ttls` entry. Both are what the two being public is for.

`raise_on` holds command NAMES; any listed command raises RedisError instead of
running, which is how each of the three failure policies gets exercised.

`aclose` is not a command and is not among the four: main.py's lifespan closes
both clients on shutdown, and the root conftest hands this fake to main.py too.
"""

from __future__ import annotations

import redis

from dodeal_ai.units.structured_intelligence.state import (
    _SLOTS_ALLOWED,
    _SLOTS_DENIED_BY_ATTEMPT,
    _SLOTS_DENIED_BY_RATE,
    _TAKE_OVER_SCRIPT,
)


class FakeOperationalRedis:
    """Async, in-memory, no clock. Attributes are public so tests assert on the
    resulting state rather than on a call log."""

    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.raise_on: set[str] = set(raise_on or ())
        # Ordered record of (command, first key) for the few assertions that
        # are genuinely about sequencing -- e.g. that a take is one EVAL.
        self.commands: list[tuple[str, str]] = []
        # (script, keys, outcome) per EVAL -- both guards' whole decision,
        # recorded on what was actually sent rather than inferred from state.
        self.evals: list[tuple[str, tuple[str, ...], int]] = []
        self.closed = False

    def _guard(self, command: str, key: str) -> None:
        self.commands.append((command, key))
        if command in self.raise_on:
            raise redis.RedisError(f"fake: {command} unavailable")

    async def set(
        self,
        name: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
        xx: bool = False,
    ) -> bool | None:
        """Returns True when the value was stored, None when NX or XX declined --
        matching redis-py, whose miss is None rather than False. XX is the
        confirm's: it only replaces, and never creates, the key."""
        self._guard("set", name)
        if nx and name in self.store:
            return None
        if xx and name not in self.store:
            return None
        self.store[name] = value
        if ex is not None:
            self.ttls[name] = ex
        return True

    async def get(self, name: str) -> str | None:
        self._guard("get", name)
        return self.store.get(name)

    def _take(self, key: str, ttl: int) -> int:
        """INCR, and the window on a key that is new or has none, as the script
        does it."""
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        if value == 1 or key not in self.ttls:
            self.ttls[key] = ttl
        return value

    async def eval(self, script: str, numkeys: int, *keys_and_args: object):
        """The prompt-slots script against the dict: [outcome, attempts, rate count],
        the attempt count AFTER a take and the rate count before it."""
        keys = tuple(str(k) for k in keys_and_args[:numkeys])
        if script == _TAKE_OVER_SCRIPT:
            return self._take_over(script, keys, keys_and_args[numkeys:])
        attempt_key, rate_key = keys
        self._guard("eval", attempt_key)
        cap, attempt_ttl, limit, rate_ttl = (
            int(str(arg)) for arg in keys_and_args[numkeys:]
        )
        attempts = int(self.store.get(attempt_key, "0"))
        rate = int(self.store.get(rate_key, "0"))
        if attempts >= cap:
            reply = [_SLOTS_DENIED_BY_ATTEMPT, attempts, 0]
        elif rate >= limit:
            reply = [_SLOTS_DENIED_BY_RATE, attempts, rate]
        else:
            taken = self._take(attempt_key, attempt_ttl)
            self._take(rate_key, rate_ttl)
            reply = [_SLOTS_ALLOWED, taken, rate]
        self.evals.append((script, keys, reply[0]))
        return reply

    def _take_over(
        self, script: str, keys: tuple[str, ...], args: tuple[object, ...]
    ) -> int:
        """The take-over script: replace the value with the reservation and its
        TTL only if it is still the value the caller read."""
        (key,) = keys
        self._guard("eval", key)
        expected, reserved, ttl = (str(arg) for arg in args)
        taken = int(self.store.get(key) == expected)
        if taken:
            self.store[key] = reserved
            self.ttls[key] = int(ttl)
        self.evals.append((script, keys, taken))
        return taken

    async def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            self._guard("delete", name)
            if self.store.pop(name, None) is not None:
                removed += 1
            self.ttls.pop(name, None)
        return removed

    async def aclose(self, close_connection_pool: bool | None = None) -> None:
        self.closed = True
