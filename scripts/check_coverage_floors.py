"""Per-file coverage floors for the modules a tenancy breach would run through.

The repo-wide gate in pyproject.toml (fail_under) is an AVERAGE: a security
module can rot to 60% while the total stays above 92% because well-covered
code elsewhere carries it. These floors are per FILE, so no file below can be
paid for by another.

The list is deliberately short -- the code on a deny path, plus the code that
decides what a salesperson is told about their own work:

    core/auth/**      Gate 1: who the caller is. Wrong -> everyone is anyone.
    core/tenancy.py   Gate 2: whose data. Wrong -> a cross-tenant read.
    core/cost/**      Gate 4: the spend cap. Wrong -> an unbounded bill.
    core/errors.py    what a denied caller is told (and is NOT told).
    core/validation.py  the untrusted-output boundary.
    core/log_safety.py  what may reach a log line.
    units/structured_intelligence/**  Unit A's judgement pipeline.
      ... /config.py   the ONLY source of a weight, threshold, cap or TTL.
                       Wrong -> a silently wrong score, not a crash.
      ... /state.py    three state concerns with three DIFFERENT failure
                       policies. Wrong -> duplicate paid work, or a user
                       pestered past their cap.

A file matched by both a `**` pattern and its own exact pattern is checked
against both and printed twice; the stricter floor governs. That is deliberate
for config.py.

100 where the file is small enough that every line is reachable in a unit
test; 95 where a defensive branch is not worth contorting a test to reach.

Run AFTER pytest, which writes coverage.json (pyproject addopts). Stdlib only:
CI runs it as a plain `uv run python scripts/check_coverage_floors.py` step and
it must not need anything pytest did not already install.
"""

from __future__ import annotations

import json
import sys
from fnmatch import fnmatch
from pathlib import Path

_COVERAGE_JSON = Path("coverage.json")

# glob pattern (forward slashes, repo-relative) -> minimum percent covered
_FLOORS: dict[str, float] = {
    "src/dodeal_ai/core/auth/**": 95,
    "src/dodeal_ai/core/tenancy.py": 100,
    "src/dodeal_ai/core/cost/**": 95,
    "src/dodeal_ai/core/errors.py": 95,
    "src/dodeal_ai/core/validation.py": 100,
    "src/dodeal_ai/core/log_safety.py": 100,
    # Unit A. The unit decides what a salesperson is told about their own work,
    # and its config is the only source of a weight or a threshold -- a gap
    # there is a silently wrong score, not a crash.
    "src/dodeal_ai/units/structured_intelligence/**": 95,
    "src/dodeal_ai/units/structured_intelligence/config.py": 100,
    "src/dodeal_ai/units/structured_intelligence/state.py": 95,
}


def _matches(pattern: str, path: str) -> bool:
    """`**` means "this directory and everything under it"; a plain path is an
    exact match. fnmatch alone would let `*` cross a directory separator, so
    the directory form is handled explicitly."""
    if pattern.endswith("/**"):
        return path.startswith(pattern[: -len("**")])
    return fnmatch(path, pattern)


def main() -> int:
    if not _COVERAGE_JSON.exists():
        print(
            f"{_COVERAGE_JSON} not found. Run `uv run pytest` first -- it writes "
            "the JSON report these floors read (pyproject addopts).",
            file=sys.stderr,
        )
        return 2

    report = json.loads(_COVERAGE_JSON.read_text(encoding="utf-8"))
    # coverage records the OS-native path; normalise so the patterns above are
    # written once and work on Windows and Linux alike.
    measured = {
        path.replace("\\", "/"): data["summary"]["percent_covered"]
        for path, data in report["files"].items()
    }

    failures = 0
    unmatched = []
    print(f"{'file':<48}{'covered':>9}{'floor':>7}  result")
    print("-" * 74)
    for pattern, floor in _FLOORS.items():
        hits = sorted(p for p in measured if _matches(pattern, p))
        if not hits:
            unmatched.append(pattern)
            continue
        for path in hits:
            percent = measured[path]
            ok = percent >= floor
            failures += not ok
            print(
                f"{path:<48}{percent:>8.2f}%{floor:>6.0f}%  {'PASS' if ok else 'FAIL'}"
            )

    if unmatched:
        # Fail closed: a renamed or deleted file must not silently drop its
        # floor. Update _FLOORS deliberately instead.
        print()
        for pattern in unmatched:
            print(f"NO FILE MATCHED: {pattern}", file=sys.stderr)
        print(
            "A floor that matches nothing is not enforcing anything. Update "
            "_FLOORS in this script to match the current layout.",
            file=sys.stderr,
        )
        return 1

    print()
    if failures:
        print(f"{failures} file(s) below their coverage floor.", file=sys.stderr)
        return 1
    print(f"All {len(_FLOORS)} coverage floors met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
