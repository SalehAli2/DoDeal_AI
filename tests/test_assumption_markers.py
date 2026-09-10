"""Marker reconciliation: a provisional answer cannot be documented in one place
and load-bearing in another.

Five questions were answered provisionally so Unit A could be built, and each
answer is marked in the code with a greppable token. The point of the token is
that the blast radius of a wrong answer is one `grep` — so the failure mode
worth preventing is a marker that goes out of sync: deleted from the code while
README still promises it, or written into a document while nothing in `src/`
actually depends on it any more.

So the invariant is a THREE-WAY one, per marker:

    at least one file under src/   the code that would change
    README.md                      what a reader is told is assumed
    ASSUMPTIONS.md                 the correction path, in the ledger

The step-3 seam marker WAS the same shape with `STATUS.md` in place of
`ASSUMPTIONS.md`, and is now the opposite test: the token pre-flight is real
(`core/cost/limiter.py::token_preflight`), so the marker must appear NOWHERE.
A three-way presence check for a seam that no longer exists would pass only
while someone kept writing the marker down, which is the failure it should
report. See `test_the_seam_marker_is_gone` below.

WHY "AT LEAST ONE UNDER src/", NOT "EXACTLY THREE FILES". The campaign brief
asked for exactly three files per marker. That is not reachable and never was:
`ASSUMPTION[Q6]` is load-bearing in both `classify.py` and `schemas.py`,
`ASSUMPTION[Q7]` in both `pipeline.py` and `state.py`, and `ASSUMPTION[Q13]` in
both `config.py` and `scoring.py` -- and several are cited in the tests that pin
the behaviour they describe, which is exactly where a reader would want them.
Forcing the count to three would mean deleting markers from code that genuinely
depends on them, to satisfy a number. The three-way presence check is the
invariant that was actually wanted; the count is recorded in
CAMPAIGN_REPORT.md, Phase J.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Markers whose ledger home is ASSUMPTIONS.md: a provisional answer with a
# correction path.
ASSUMPTION_MARKERS = [
    "ASSUMPTION[Q1]",
    "ASSUMPTION[Q6]",
    "ASSUMPTION[Q7]",
    "ASSUMPTION[Q8]",
    "ASSUMPTION[Q13]",
]
# RETIRED. The stub it named is gone (Piece N.2): `token_preflight` is a real
# fail-open read and the pre-flight is enforced. Assembled rather than spelled,
# because the assertion below is that NO tracked file carries it -- and a file
# that wrote it out whole would be the first one to fail its own test.
SEAM_MARKER = "SEAM" + "[STEP3]"

# CAMPAIGN_REPORT.md is excluded because it QUOTES every marker while discussing
# the work, so counting it would make the report's own prose satisfy the
# invariant. docs/campaign/ is the campaign specification, which quotes them for
# the same reason; docs/audit/ is a point-in-time sweep, not a live document.
_EXCLUDED = (
    ":(exclude)CAMPAIGN_REPORT.md",
    ":(exclude)docs/campaign/",
    ":(exclude)docs/audit/",
)


def _files_containing(marker: str) -> set[str]:
    """Repo-relative paths of every TRACKED file containing `marker`.

    `git grep` rather than a walk: it already honours .gitignore, skips .git and
    .venv, and searching only tracked files is the right scope -- an untracked
    scratch file mentioning a marker is not documentation.
    """
    result = subprocess.run(
        ["git", "grep", "-l", "--fixed-strings", marker, "--", ".", *_EXCLUDED],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # git grep exits 1 with no output when nothing matched; that is a real
    # answer (the empty set), not an error. Any other non-zero is a failure.
    if result.returncode not in (0, 1):
        raise AssertionError(f"git grep failed for {marker}: {result.stderr}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


@pytest.mark.parametrize("marker", ASSUMPTION_MARKERS)
def test_every_assumption_marker_is_in_src_readme_and_assumptions(marker):
    found = _files_containing(marker)
    assert found, f"{marker} appears nowhere -- deleted without updating the docs?"

    in_src = sorted(f for f in found if f.startswith("src/"))
    assert in_src, (
        f"{marker} is documented but nothing under src/ carries it. Either the "
        f"code no longer depends on the assumption -- in which case retire it "
        f"from README.md and ASSUMPTIONS.md -- or the marker was dropped in an "
        f"edit. Found in: {sorted(found)}"
    )
    assert "README.md" in found, (
        f"{marker} is load-bearing in {in_src} but README.md's "
        f"provisional-answers table does not mention it."
    )
    assert "ASSUMPTIONS.md" in found, (
        f"{marker} is load-bearing in {in_src} but ASSUMPTIONS.md carries no "
        f"entry, so there is no recorded correction path."
    )


def test_the_seam_marker_is_gone():
    """The seam is closed, so the marker must appear in no tracked file."""
    found = _files_containing(SEAM_MARKER)

    assert not found, (
        f"{SEAM_MARKER} is retired -- the token pre-flight has been real since "
        f"Piece N.2 (core/cost/limiter.py::token_preflight). A file still "
        f"carrying it is describing a stub that no longer exists: "
        f"{sorted(found)}. Delete the marker and say what the code does now."
    )


def test_the_marker_set_is_the_five_the_campaign_answered():
    # A sixth ASSUMPTION[...] appearing in src/ without an entry above means
    # someone answered a question provisionally and did not add it to the
    # ledger. This test is how that gets noticed.
    result = subprocess.run(
        ["git", "grep", "-ho", "-E", r"ASSUMPTION\[Q[0-9]+\]", "--", "src/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    in_code = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    assert in_code == set(ASSUMPTION_MARKERS), (
        f"src/ carries {sorted(in_code)}; this file tracks "
        f"{sorted(ASSUMPTION_MARKERS)}. Add the new one here, to README.md and "
        f"to ASSUMPTIONS.md, or remove it from the code."
    )
