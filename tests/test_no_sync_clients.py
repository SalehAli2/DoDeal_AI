"""No synchronous client, and no sleep, anywhere under src/.

Register item 75. This service is one event loop. A blocking call inside it does
not slow down the request that made it — it stops EVERY request in the process,
including the ones that were nearly finished, including `/health`, and including
the load-shed middleware's own arithmetic. One `requests.get()` against a
backend having a bad day is a whole pod that stops answering, and nothing about
the code that did it looks wrong: it is the ordinary spelling of an HTTP call.

The async spellings all exist already. `httpx.AsyncClient` behind
`tools/httpx_transport.py`, `redis.asyncio` behind `core/redis.py`, and
`asyncio.sleep` if a delay is ever needed. So a hit here is never "we had no
choice"; it is a habit, and the point of a grep is to catch it in review rather
than in production.

`time.sleep` and `urllib.request` are on the list for the same reason as the
clients even though neither is a client: they block the loop identically. The
audit's Category I sweep already forbids `time.sleep` on a determinism ground
(docs/audit/2026-09-05-sweep.md); it is here on a concurrency one, and both
reasons want it gone.

Styled on the shipped-template greps in `tests/unit/test_scoring.py` and
`tests/security/test_unit_a_injection.py`: read the files, search for a fixed
set of patterns, and name the file and line when one is found.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_SRC = pathlib.Path("src/dodeal_ai")
_MODULES = sorted(_SRC.rglob("*.py"))

# Each entry is (pattern, what to do instead). The advice is in the failure
# message on purpose: a test that only says "forbidden" sends the next person
# to git blame.
_FORBIDDEN: tuple[tuple[str, str], ...] = (
    (r"^\s*import requests\b", "httpx.AsyncClient via tools/httpx_transport.py"),
    (r"\brequests\.", "httpx.AsyncClient via tools/httpx_transport.py"),
    (r"\bhttpx\.Client\(", "httpx.AsyncClient"),
    (r"\bredis\.Redis\(", "redis.asyncio via core/redis.py"),
    (r"\bredis\.StrictRedis\(", "redis.asyncio via core/redis.py"),
    (r"\btime\.sleep\(", "await asyncio.sleep(...)"),
    (r"\burllib\.request\b", "httpx.AsyncClient via tools/httpx_transport.py"),
)


def test_the_package_has_modules_to_search():
    # Fail closed on a rename. A glob that matched nothing would make every test
    # below pass by searching an empty set — the quietest way for a guard to
    # stop guarding.
    assert len(_MODULES) > 20, f"only {len(_MODULES)} modules under {_SRC}"


@pytest.mark.parametrize(("pattern", "instead"), _FORBIDDEN, ids=lambda x: x)
def test_no_module_under_src_uses_a_blocking_client(pattern: str, instead: str):
    compiled = re.compile(pattern)
    hits = [
        f"{module.as_posix()}:{number}: {line.strip()}"
        for module in _MODULES
        for number, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        )
        if compiled.search(line)
    ]
    assert not hits, (
        f"blocking call in the event loop -- use {instead} instead:\n" + "\n".join(hits)
    )
