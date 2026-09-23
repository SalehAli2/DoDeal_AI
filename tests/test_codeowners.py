"""CODEOWNERS (register item 156): @SalehAli2 owns everything but Unit B's unit
package, which the last rule leaves unowned -- and that rule names a directory
that exists, so a rename cannot silently hand the unit back."""

from __future__ import annotations

import pathlib

_CODEOWNERS = pathlib.Path(".github/CODEOWNERS")
_UNIT_B = "/src/dodeal_ai/units/call_intelligence/"


def _rules() -> list[tuple[str, list[str]]]:
    rules = []
    for line in _CODEOWNERS.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            pattern, *owners = line.split()
            rules.append((pattern, owners))
    return rules


def _owners(path: str) -> list[str]:
    """GitHub's rule for the two pattern shapes used here: `*` matches every
    path, a leading-slash directory matches what is under it; the last wins."""
    owners: list[str] = []
    for pattern, rule_owners in _rules():
        if pattern == "*" or ("/" + path).startswith(pattern):
            owners = rule_owners
    return owners


def test_the_rules_are_everything_owned_and_unit_b_unowned() -> None:
    assert _rules() == [("*", ["@SalehAli2"]), (_UNIT_B, [])]


def test_unit_b_is_unowned_and_its_core_pieces_are_owned() -> None:
    assert _owners("src/dodeal_ai/units/call_intelligence/worker.py") == []
    for path in (
        "src/dodeal_ai/core/audio_download.py",
        "src/dodeal_ai/core/jobs.py",
        "src/dodeal_ai/api/routes/calls.py",
        "src/dodeal_ai/workers/calls.py",
        "src/dodeal_ai/units/structured_intelligence/pipeline.py",
        ".github/CODEOWNERS",
    ):
        assert _owners(path) == ["@SalehAli2"], path


def test_the_unowned_pattern_names_a_real_directory() -> None:
    assert pathlib.Path(_UNIT_B.strip("/")).is_dir()
