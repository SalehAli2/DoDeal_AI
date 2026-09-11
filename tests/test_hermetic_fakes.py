"""Every Redis client a test injects is a shared fake from tests/helpers/ or fakeredis
(the N.2 finding)."""

from __future__ import annotations

import ast
import pathlib

# Allowed: a shared fake from tests/helpers/, a fakeredis client (real Lua, no
# socket), or a name bound to either. core/redis.py's own factory tests are skipped.
_TESTS = pathlib.Path("tests")
_PATCHED_NAMES = {"get_cost_client", "get_operational_client"}
# The module whose own tests legitimately hand these factories something else.
_FACTORY_MODULE = "dodeal_ai.core.redis"
_HELPER_PACKAGE = "tests.helpers"
_REAL_REDIS_PACKAGE = "fakeredis"


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _approved_names(tree: ast.Module) -> set[str]:
    """Names in this file that stand for a sanctioned fake, grown from the imports to a
    fixpoint."""
    approved = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith((_HELPER_PACKAGE, _REAL_REDIS_PACKAGE)):
                approved |= {alias.asname or alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_REAL_REDIS_PACKAGE):
                    approved.add(alias.asname or alias.name.split(".")[0])

    bodies: list[tuple[set[str], ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            bodies.append((targets, node.value))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            # A fixture that builds one IS one, as far as `lambda: client` can
            # tell from the outside.
            bodies.append(({node.name}, node))

    grew = True
    while grew:
        grew = False
        for targets, value in bodies:
            if targets <= approved:
                continue
            if _names_in(value) & approved:
                approved |= targets
                grew = True
    return approved


def _module_alias_map(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted module path, for `import x as y` and `from x import y`."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _patch_calls(tree: ast.Module) -> list[ast.Call]:
    """Calls that replace one of the two factory names on some module."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        target = node.args[1]
        if isinstance(target, ast.Constant) and target.value in _PATCHED_NAMES:
            calls.append(node)
    return calls


def test_every_patched_redis_client_is_a_shared_fake() -> None:
    offenders = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        approved = _approved_names(tree)
        aliases = _module_alias_map(tree)
        for call in _patch_calls(tree):
            patched_module = call.args[0]
            if (
                isinstance(patched_module, ast.Name)
                and aliases.get(patched_module.id, "") == _FACTORY_MODULE
            ):
                continue
            value_names: set[str] = set()
            for node in [*call.args[2:], *(kw.value for kw in call.keywords)]:
                value_names |= _names_in(node)
            if not value_names & approved:
                offenders.append(
                    f"{path.as_posix()}:{call.lineno} "
                    f"{call.args[1].value} <- {ast.unparse(call)[:60]}"
                )

    assert not offenders, (
        "a Redis client was faked with something that is not the shared helper "
        "in tests/helpers/ (or fakeredis). A local copy drifts from the real "
        "client's semantics, and six of them is how the last one was found:\n"
        + "\n".join(offenders)
    )
