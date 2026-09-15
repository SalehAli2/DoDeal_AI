"""No BaseHTTPMiddleware anywhere under src/.

Register item 86. The two middlewares in this service were both
`starlette.middleware.base.BaseHTTPMiddleware`, and that base class is not free:
it runs EVERY request through an anyio task group and a pair of memory object
streams so that `dispatch` can be written against a Request and return a
Response. What that buys is a convenient signature. What it costs is

  - a task group per request, on a load-shed middleware whose entire
    justification is that a refusal is "a counter comparison and nothing more";
  - the route running in a CHILD task, so a contextvars.ContextVar set in a
    route is invisible to anything outside the middleware -- the property
    tests/unit/test_middleware_asgi.py pins from the other side;
  - cancellation behaviour that has moved between Starlette releases, which a
    pin defers rather than fixes.

The pure-ASGI spelling is a few lines longer and has none of that: `__init__`
takes the inner app, `__call__` takes `(scope, receive, send)`. So a hit here is
never "we had no choice"; it is the convenient import, and the point of a grep
is to catch it in review rather than in a flame graph.

THE PATTERNS ARE IMPORT-SHAPED AND CLASS-SHAPED, not the bare name. Both
middleware modules NAME the class in their docstrings, because "why this is not
a BaseHTTPMiddleware" is the one thing a reader of the new shape most needs
told; a grep for the bare word would forbid the explanation along with the
thing. So: the two import spellings, and the inheritance itself, which is what
an alias (`from starlette.middleware import base`) still has to write out.

Styled on tests/test_no_sync_clients.py: read the files, search for a fixed set
of patterns, and name the file and line when one is found.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_SRC = pathlib.Path("src/dodeal_ai")
_MODULES = sorted(_SRC.rglob("*.py"))

_INSTEAD = (
    "a plain ASGI callable: __init__(self, app) and "
    "async def __call__(self, scope, receive, send)"
)

_FORBIDDEN: tuple[str, ...] = (
    r"^\s*from\s+starlette\.middleware\.base\s+import\b",
    r"^\s*import\s+starlette\.middleware\.base\b",
    r"^\s*class\s+\w+\s*\([^)]*\bBaseHTTPMiddleware\b",
    r"^\s*from\s+starlette\.middleware\.base\s+import\b.*\bRequestResponseEndpoint\b",
)


def test_the_package_has_modules_to_search():
    # Fail closed on a rename. A glob that matched nothing would make the test
    # below pass by searching an empty set.
    assert len(_MODULES) > 20, f"only {len(_MODULES)} modules under {_SRC}"


def test_the_middleware_modules_are_among_them():
    # And fail closed on the one directory this test is actually about: if
    # middleware/ moved, the grep above would still pass while guarding nothing.
    names = {module.as_posix() for module in _MODULES}
    assert "src/dodeal_ai/middleware/inflight.py" in names
    assert "src/dodeal_ai/middleware/request_id.py" in names


@pytest.mark.parametrize("pattern", _FORBIDDEN, ids=lambda x: x)
def test_no_module_under_src_uses_base_http_middleware(pattern: str):
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
        f"BaseHTTPMiddleware under src/ -- write {_INSTEAD} instead:\n"
        + "\n".join(hits)
    )


def test_the_patterns_would_catch_the_shape_they_forbid(tmp_path):
    """The guard guarding itself: the three spellings this test exists to
    refuse must actually match, or it is a grep that passes for the wrong
    reason -- the failure mode a pattern typo produces silently."""
    would_hit = (
        "from starlette.middleware.base import BaseHTTPMiddleware",
        "import starlette.middleware.base",
        "class InflightMiddleware(BaseHTTPMiddleware):",
        "    class Nested(base.BaseHTTPMiddleware):",
    )
    compiled = [re.compile(pattern) for pattern in _FORBIDDEN]
    for line in would_hit:
        assert any(pattern.search(line) for pattern in compiled), line

    # And prose naming the class is NOT a hit: both middleware docstrings say
    # why they are not one, and that sentence has to survive the guard.
    prose = "    It was a BaseHTTPMiddleware, which runs every request through"
    assert not any(pattern.search(prose) for pattern in compiled)
