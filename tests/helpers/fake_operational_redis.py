"""FakeOperationalRedis — the in-memory stand-in for the db2 connection.

Lives here, not inside one test module, because the state tests, the route
tests (Phase C) and the pipeline tests all need it. Injected the way the cost
tests inject theirs:

    monkeypatch.setattr(state, "get_operational_client", lambda: fake)

Implements exactly the seven commands units/structured_intelligence/state.py
issues -- SET (with NX/XX/EX), GET, INCR, TTL, EXPIRE, DELETE and the rate
limit's EVAL -- and no more. A command the unit does not use is a command whose fake
semantics nobody has checked against real Redis.

EVAL mirrors the rate-limit script against the same dict and records each call
in `evals` as (script, key, allowed); the real script runs in test_cost_lua.py.

NO CLOCK. `ex` and `expire` record a TTL in `ttls` and nothing ever counts it
down, so a test asserting "the window was set to 3600" reads `fake.ttls[key]`
rather than waiting. Expiry is therefore simulated by deleting a key, and TTL
== -1 (the audit-M4 edge: a key with no expiry) is simulated by setting the
entry directly. Both are what `store` and `ttls` being public is for.

`raise_on` holds command NAMES; any listed command raises RedisError instead of
running, which is how each of the three failure policies gets exercised.
"""

from __future__ import annotations

import redis

# Redis' own sentinels, reproduced exactly: TTL returns -2 for a key that does
# not exist and -1 for a key with no expiry set.
TTL_KEY_ABSENT = -2
TTL_NO_EXPIRY = -1


class FakeOperationalRedis:
    """Async, in-memory, no clock. Attributes are public so tests assert on the
    resulting state rather than on a call log."""

    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.raise_on: set[str] = set(raise_on or ())
        # Ordered record of (command, key) for the few assertions that are
        # genuinely about sequencing -- e.g. that EXPIRE was not issued on a
        # key that already had a TTL.
        self.commands: list[tuple[str, str]] = []
        # (script, key, allowed) per EVAL -- the rate limit's whole decision,
        # recorded on what was actually sent rather than inferred from state.
        self.evals: list[tuple[str, str, bool]] = []

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

    async def incr(self, name: str) -> int:
        self._guard("incr", name)
        # Redis creates a missing key at 0 before incrementing, and leaves it
        # with NO expiry -- which is exactly the state _incr_with_window has to
        # notice.
        value = int(self.store.get(name, "0")) + 1
        self.store[name] = str(value)
        return value

    async def ttl(self, name: str) -> int:
        self._guard("ttl", name)
        if name not in self.store:
            return TTL_KEY_ABSENT
        return self.ttls.get(name, TTL_NO_EXPIRY)

    async def expire(self, name: str, seconds: int) -> bool:
        self._guard("expire", name)
        if name not in self.store:
            return False
        self.ttls[name] = seconds
        return True

    async def eval(self, script: str, numkeys: int, *keys_and_args: object):
        """The rate-limit script against the dict, returning [allowed, count before this
        call]."""
        key = str(keys_and_args[0])
        self._guard("eval", key)
        limit = int(str(keys_and_args[numkeys]))
        ttl = int(str(keys_and_args[numkeys + 1]))
        count = int(self.store.get(key, "0"))
        allowed = count < limit
        if allowed:
            self.store[key] = str(count + 1)
            # The script's own condition: new key, or one that somehow has no
            # expiry (TTL == -1, which here is simply no entry in `ttls`).
            if count == 0 or key not in self.ttls:
                self.ttls[key] = ttl
        self.evals.append((script, key, allowed))
        return [1 if allowed else 0, count]

    async def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            self._guard("delete", name)
            if self.store.pop(name, None) is not None:
                removed += 1
            self.ttls.pop(name, None)
        return removed
