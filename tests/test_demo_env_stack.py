"""The demo's env-file order is the same in Compose and in the token minter.

WHY THIS EXISTS. `scripts/mint_demo_token.py` SIGNS a token; the service
VERIFIES it. Both read `DODEAL_JWT_SIGNING_KEY`, and both read it from a stack
of env files where a LATER file wins. The stacks are defined in two places that
know nothing about each other -- `DEFAULT_ENV_STACK` in the script, and the
`env_file:` lists Compose concatenates across `docker-compose.yml` and
`docker-compose.demo.yml`.

If those two orders ever diverge -- a file added to one, the demo file moved
ahead of `.env`, a rename done in one place -- the script signs with one key
and the service verifies with another. Nothing errors at startup. Every minted
token comes back from Gate 1 as a generic **401**, reason `invalid_token`,
which is indistinguishable at the client from a forged token and reads exactly
like a broken gate. **That misdiagnosis is what this test exists to prevent**,
and it is worth a test precisely because the symptom points away from the
cause.

WHY NO YAML PARSER. `pyyaml` is installed here, but only as a transitive
dependency of `uvicorn[standard]` and `pre-commit` -- this project declares it
nowhere. A guard built on an undeclared transitive is a guard that disappears
the day a dependency is dropped. What is being pinned is two literal list
entries, not YAML semantics, so a line scan does it with nothing at all. The
scan FAILS CLOSED: an unparseable block yields no entries and the comparison
below fails, rather than passing over an empty list.
"""

from __future__ import annotations

import pathlib

from scripts.mint_demo_token import DEFAULT_ENV_STACK

# In `-f` order, which is the order the demo is documented and run in:
#   docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build
# Compose concatenates the `env_file:` lists across these, later winning, so
# this sequence IS the container's stack.
_COMPOSE_FILES = (
    pathlib.Path("docker-compose.yml"),
    pathlib.Path("docker-compose.demo.yml"),
)

_SERVICE = "api"


def _env_file_paths(text: str) -> list[str]:
    """The `env_file:` list entries of a compose file, in order.

    Handles both forms Compose accepts -- `- .env` and the long
    `- path: .env` / `required: false` -- and stops at the first line that
    dedents back to or past the `env_file:` key. Returns [] for anything it
    does not recognise, including flow style (`env_file: [.env]`), which the
    caller treats as a failure rather than as an empty list.
    """
    paths: list[str] = []
    block_indent: int | None = None

    for raw in text.splitlines():
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())

        if block_indent is None:
            if stripped == "env_file:":
                block_indent = indent
            continue

        if not stripped or stripped.startswith("#"):
            continue
        if indent <= block_indent:
            break  # dedented out of the block

        if stripped.startswith("- path:"):
            paths.append(stripped[len("- path:") :].strip())
        elif stripped.startswith("- "):
            paths.append(stripped[2:].strip())
        # anything else inside the block (`required: false`) is not a path

    return paths


def _compose_stack() -> list[str]:
    stack: list[str] = []
    for path in _COMPOSE_FILES:
        assert path.is_file(), f"{path} is missing"
        stack.extend(_env_file_paths(path.read_text(encoding="utf-8")))
    return stack


def test_the_parser_reads_both_compose_list_forms() -> None:
    """A test of the scanner itself, so the pin below cannot pass because the
    scanner quietly stopped matching and returned a list that happens to fit."""
    sample = """services:
  api:
    env_file:
      - path: .env
        required: false
      - .env.second
    environment:
      NOT_AN_ENV_FILE: "1"
  other:
    image: x
"""
    assert _env_file_paths(sample) == [".env", ".env.second"]
    assert _env_file_paths("services:\n  api:\n    image: x\n") == []


def test_each_compose_file_contributes_at_least_one_env_file() -> None:
    """The tripwire. A reformat to flow style, a rename, or a scanner that
    stopped matching would otherwise leave the pin comparing two empty lists."""
    for path in _COMPOSE_FILES:
        found = _env_file_paths(path.read_text(encoding="utf-8"))
        assert found, (
            f"no env_file entries parsed out of {path} -- either the file "
            "stopped declaring one, or it was rewritten in a form this scan "
            "does not read (flow style, an anchor). Fix the scan; do not "
            "delete this assertion."
        )


def test_compose_env_file_order_matches_the_minter_default_stack() -> None:
    """The one that matters. Same files, same order, both places."""
    assert _compose_stack() == list(DEFAULT_ENV_STACK), (
        "the Compose env-file stack and scripts/mint_demo_token.py's "
        "DEFAULT_ENV_STACK have diverged. Every token the minter prints will "
        "be signed with a key the service does not verify with, and it will "
        "surface as a generic 401 invalid_token that looks like a broken gate. "
        "Change both, in the same commit."
    )


def test_the_demo_file_is_last_so_it_wins() -> None:
    """Stated separately from the equality above because it is the PROPERTY,
    not the values: a later env file wins, so the demo's invented signing key
    must sit after whatever a developer's own `.env` sets."""
    assert _compose_stack()[-1] == ".env.demo"
    assert DEFAULT_ENV_STACK[-1] == ".env.demo"


def test_the_service_the_stack_belongs_to_is_still_named_api() -> None:
    """The scan is service-blind: it takes the first `env_file:` in each file.
    That is only correct while `api` is the only service declaring one."""
    for path in _COMPOSE_FILES:
        text = path.read_text(encoding="utf-8")
        assert text.count("env_file:") <= 1, (
            f"{path} declares more than one env_file: block, so the scan can no "
            "longer assume the first one is the service's"
        )
        if "env_file:" in text:
            assert f"{_SERVICE}:" in text
