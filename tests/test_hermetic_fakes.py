"""Every Redis client a test injects is a shared fake from tests/helpers/ or fakeredis
(the N.2 finding), and no test reaches a real one (register item 103, the N.4
finding). And no test reads the developer's `.env` (register item 115)."""

from __future__ import annotations

import ast
import importlib
import os
import pathlib
from urllib.parse import urlsplit

import pytest

from dodeal_ai import main
from dodeal_ai.core import redis as redis_module
from dodeal_ai.core.config import Settings, _build_settings, get_settings
from dodeal_ai.core.cost import limiter
from tests.helpers.fake_cost_redis import FakeCostRedis

# Allowed: a shared fake from tests/helpers/, a fakeredis client (real Lua, no
# socket), or a name bound to either. core/redis.py's own factory tests are skipped.
_TESTS = pathlib.Path("tests")
_SRC = pathlib.Path("src")
_LANE = _TESTS / "redis_real"
# The importers known today. The scan must find at least these, or it is looking
# in the wrong place and would pass for the wrong reason.
_KNOWN_HOLDERS = {
    "dodeal_ai.core.cost.limiter",
    "dodeal_ai.units.structured_intelligence.state",
    "dodeal_ai.main",
}
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


# --- no test reaches a real client (register item 103) ---------------------


def _factory_bindings() -> list[tuple[str, str, str]]:
    """(module, factory, local name) for every src module that imports a factory by
    name. Each holds its own reference, which no patch on core/redis.py reaches."""
    bindings = []
    for path in sorted((_SRC / "dodeal_ai").rglob("*.py")):
        parts = path.relative_to(_SRC).with_suffix("").parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == _FACTORY_MODULE:
                bindings += [
                    (module, alias.name, alias.asname or alias.name)
                    for alias in node.names
                    if alias.name in _PATCHED_NAMES
                ]
    return bindings


def test_no_test_reaches_a_real_redis_client_factory(redis_fakes) -> None:
    """The default run cannot reach a real Redis client, whether or not one is
    listening.

    Inside a test, every name a src module binds to a factory is the root
    conftest's fake, and core/redis.py keeps the real factories for its own
    tests. Every real pool build is counted, and the root conftest's
    pytest_sessionfinish fails the run if a test outside tests/unit/test_redis.py
    made one. No test can see that count at the end of the session, so this one
    proves the counter is in place.
    """
    bindings = _factory_bindings()
    assert {module for module, _, _ in bindings} >= _KNOWN_HOLDERS, (
        "the scan found fewer factory importers than are known to exist"
    )

    fake_for = {
        "get_cost_client": redis_fakes.cost,
        "get_operational_client": redis_fakes.operational,
    }
    unreplaced = []
    for module_name, factory, local in bindings:
        bound = getattr(importlib.import_module(module_name), local)
        # Identity first: calling the real factory would build a real pool.
        if bound is getattr(redis_module, factory) or bound() is not fake_for[factory]:
            unreplaced.append(f"{module_name}.{local}")
    assert not unreplaced, (
        "a src module imports a Redis factory by name and the root conftest does "
        "not replace it; add the module to _COST_CLIENT_HOLDERS or "
        "_OPERATIONAL_CLIENT_HOLDERS in tests/conftest.py:\n" + "\n".join(unreplaced)
    )

    assert hasattr(redis_module.get_cost_client, "cache_clear")
    assert hasattr(redis_module.get_operational_client, "cache_clear")
    assert hasattr(redis_module._build_pool, "__wrapped__"), (
        "core/redis.py's _build_pool is not wrapped by the session's counter"
    )


def test_the_service_urls_point_at_a_closed_port_and_the_lane_keeps_its_own() -> None:
    """The second line of defence. A client that bypassed the fakes would connect to
    a closed loopback port, never to a Redis someone has running.

    The redis_real lane is unaffected by construction: it reads
    DODEAL_REDIS_REAL_URL and names neither service URL, as a variable or as a
    Settings field.
    """
    settings = get_settings()
    for variable, url in (
        ("DODEAL_REDIS_COST_URL", settings.redis_cost_url),
        ("DODEAL_REDIS_OPERATIONAL_URL", settings.redis_operational_url),
    ):
        assert os.environ[variable] == url
        parts = urlsplit(url)
        assert (parts.hostname, parts.port) == ("127.0.0.1", 1), variable

    lane = "\n".join(p.read_text(encoding="utf-8") for p in sorted(_LANE.rglob("*.py")))
    assert "DODEAL_REDIS_REAL_URL" in lane
    for service_name in (
        "DODEAL_REDIS_COST_URL",
        "DODEAL_REDIS_OPERATIONAL_URL",
        "redis_cost_url",
        "redis_operational_url",
    ):
        assert service_name not in lane, service_name


@pytest.fixture
def own_cost(monkeypatch) -> FakeCostRedis:
    """A module's own db1 fake, installed the way the pipeline modules install theirs."""
    client = FakeCostRedis()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: client)
    return client


def test_a_module_patch_wins_over_the_default_fakes(own_cost, redis_fakes) -> None:
    """The root conftest's fakes are a default, not an override.

    Its autouse fixture is set up before any fixture a module defines, even one a
    test lists first, as this test does. Both patch through one monkeypatch, so
    the module's patch lands second. A name the module did not patch keeps the
    default.
    """
    assert limiter.get_cost_client() is own_cost
    assert own_cost is not redis_fakes.cost
    assert main.get_cost_client() is redis_fakes.cost


# --- .env is not a source for any test (register item 115) ------------------


def test_env_file_is_cut_out_of_settings_for_the_whole_session() -> None:
    """The fixture's own property, asserted rather than assumed.

    `tests/conftest.py::_no_dotenv` clears it session-wide. Asserted directly
    so that deleting that fixture fails HERE, with a sentence explaining what
    broke, rather than somewhere downstream on whichever machine happens to
    have a populated `.env`.
    """
    assert Settings.model_config.get("env_file") is None


def test_a_dotenv_in_the_working_directory_cannot_reach_settings(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real defect, reproduced: a populated file where Settings looks.

    `env_file=".env"` resolves against the WORKING DIRECTORY, so a file written
    here and a chdir is exactly the situation that turned the suite red -- not
    an approximation of it. Every value below is deliberately one a test
    elsewhere asserts the absence of.
    """
    (tmp_path / ".env").write_text(
        "DODEAL_JWT_SIGNING_KEY=key-from-the-file\n"
        'DODEAL_DD_API_KEYS={"leaked-tenant":"leaked-key"}\n'
        "DODEAL_LLM_PROVIDER=groq\n"
        "DODEAL_LLM_MODEL=leaked-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "key-from-the-environment")

    settings = _build_settings()

    assert settings.dd_api_keys == {}
    assert settings.llm_provider is None
    assert settings.llm_model == ""
    # The environment still works; it is the FILE that is cut out.
    assert settings.jwt_signing_key == "key-from-the-environment"


def test_a_dict_setting_is_not_merged_from_a_dotenv(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that setenv could never have defended.

    For a DICT field pydantic-settings MERGES sources instead of letting the
    environment replace the file, so before item 115 a test that set
    DODEAL_DD_API_KEYS by hand received the file's entries merged in on top --
    `{'localtenant', 't1', 't2'}` where it asserted `{'t1', 't2'}`. Setting the
    value explicitly is the strongest thing a test can do, and it was not
    enough; only removing the file from the sources is.
    """
    (tmp_path / ".env").write_text(
        'DODEAL_DD_API_KEYS={"leaked-tenant":"leaked-key"}', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_DD_API_KEYS", '{"t1":"k1"}')

    settings = _build_settings()

    assert set(settings.dd_api_keys) == {"t1"}
    assert "leaked-tenant" not in settings.dd_api_keys


def test_the_explicit_env_file_seam_still_works(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cutting `.env` out must not break reading a file somebody ASKED for.

    `_build_settings(_env_file=...)` is the correct seam and is not the defect;
    a fixture that broke it would have replaced one silent failure mode with
    another.
    """
    named = tmp_path / "asked-for.env"
    named.write_text("DODEAL_JWT_SIGNING_KEY=key-from-the-named-file", encoding="utf-8")
    monkeypatch.delenv("DODEAL_JWT_SIGNING_KEY", raising=False)

    settings = _build_settings(_env_file=named)

    assert settings.jwt_signing_key == "key-from-the-named-file"
