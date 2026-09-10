"""In-memory stand-in for the cost/quota connection (db1).

Two counters now live on this connection -- Gate 4's per-request `cost:*` and
the token budget's `tokens:*` -- and both are moved by an EVAL and read by an
MGET. Before the token pre-flight was real, a test that drove the pipeline
without patching this client touched no Redis at all; now it would open a
socket. So the fake exists once here rather than as a fourth copy of the
`_FakeCostRedis` that three test modules already carry.

It reproduces what the scripts do against a dict: no Lua interpreter, no TTL
semantics, no cross-worker sharing. `tests/unit/test_cost_lua.py` is where the
real scripts run, on `fakeredis[lua]`; this is for the tests whose subject is
something else and which only need the store not to be the thing that fails.

WHAT IT RECORDS, AND WHY. Every EVAL appends (script, keys) to `evals`. That is
what lets a test assert the two scripts touch DISJOINT key sets from the
outside, on what was actually sent, rather than by re-reading the two constants
and trusting they are the ones the code used.
"""

from __future__ import annotations

import redis


class FakeCostRedis:
    """The two calls the limiter makes: EVAL of an add-both script, and MGET."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, int] = {}
        # Set True to make every command raise the way a dead server does --
        # the fail-open path both the charge and the pre-flight take.
        self.fail = fail
        self.evals: list[tuple[str, tuple[str, ...]]] = []
        self.mgets: list[tuple[str, ...]] = []

    def keys_for(self, script: str) -> set[str]:
        """Every key `script` was EVAL'd against, across all calls."""
        return {key for source, keys in self.evals if source == script for key in keys}

    async def eval(self, script: str, numkeys: int, *keys_and_args: object):
        if self.fail:
            raise redis.RedisError("down")
        keys = tuple(str(k) for k in keys_and_args[:numkeys])
        # ARGV[1] is the amount; ARGV[2] is the window, which has no TTL
        # semantics to model here.
        amount = int(str(keys_and_args[numkeys]))
        self.evals.append((script, keys))
        totals = []
        for key in keys:
            self.store[key] = self.store.get(key, 0) + amount
            totals.append(self.store[key])
        return totals

    async def mget(self, *keys: str):
        if self.fail:
            raise redis.RedisError("down")
        self.mgets.append(tuple(keys))
        return [self.store.get(key) for key in keys]
