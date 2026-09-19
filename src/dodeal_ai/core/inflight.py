"""The process's in-flight request count, readable from every layer.

Register item 13. middleware/inflight.py admits and sheds requests against this
counter, and the Unit A pipeline reads it for its outcome lines. It lives in core
so a unit reads it without importing the middleware layer above it; the layer
rule in pyproject.toml ([tool.importlinter]) keeps it that way.
"""

from __future__ import annotations


class InflightCounter:
    """How many requests are inside the app right now.

    A plain integer behind three methods rather than a bare module global,
    because the thing that has to be true of it is that every increment is
    matched by a decrement — and a named object with `release()` on it is
    something a reader can check, whereas `_count -= 1` scattered across a
    dispatch method is something a reader has to trace.

    NO LOCK, deliberately. This counts work on ONE event loop in ONE process:
    the read-compare-increment below never awaits between its steps, so no other
    task can run inside it and there is nothing for a lock to protect. (It is
    per-process for the same reason it is exact — two pods each shed against
    their own ceiling, which is what a per-pod ceiling means.)
    """

    __slots__ = ("_count",)

    def __init__(self) -> None:
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def acquire(self, limit: int) -> bool:
        """Take a slot if one is free. True = admitted, False = shed."""
        if self._count >= limit:
            return False
        self._count += 1
        return True

    def release(self) -> None:
        self._count -= 1


# Module-level and shared by the whole process, which is what "in flight" means.
# It is also how the pipeline reads the number for its outcome line: a unit has
# a TenantScope and no Request, so it cannot reach app.state.
_counter = InflightCounter()


def current_inflight() -> int:
    """The count as of right now, for anything that wants to record it.

    A snapshot and nothing more. It is an observation about the process at the
    moment it was taken, never an input to a decision -- the only code allowed
    to decide on this number is `acquire` above, which reads and acts on it
    without an await in between.
    """
    return _counter.count
