"""No helper module defines a function pytest would collect (register item 152).

A `test*` function in tests/helpers/ becomes a test in every module that imports
it by name: it runs, asserts nothing, and returns a value pytest warns about.
"""

from __future__ import annotations

import ast
from pathlib import Path

HELPERS = Path(__file__).parent / "helpers"


def test_no_helper_defines_a_collectable_function() -> None:
    """Top-level functions in non-test helper modules never start with `test`."""
    modules = [
        path for path in HELPERS.glob("*.py") if not path.name.startswith("test")
    ]
    assert modules
    offenders = [
        f"{path.name}:{node.name}"
        for path in modules
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test")
    ]
    assert offenders == []
