"""The configured scored set is well formed — and skips entirely without one.

THIS IS THE "UNSET MEANS SKIP" GUARD, and it is a test rather than a promise in
a docstring: with `DODEAL_EVAL_SET_PATH` unset -- which is CI, and which is
every developer who has not deliberately pointed at a set -- the whole module
skips and no file is opened. With it set, this is the check the lead runs
before a marking session: does the file still parse, and how much of it has
actually been marked.

IT PRINTS COUNTS AND NEVER TEXT. Every assertion here is about shape and
totals. A scored set holds real notes, so nothing in this file may put one on
a terminal, in a failure message or in a CI log.

It makes NO model call. Whether the AI agrees with these marks is
scripts/run_eval.py's question, and that one costs money.
"""

from __future__ import annotations

import pytest

from dodeal_ai.units.structured_intelligence.eval_set import (
    EVAL_SET_PATH_ENV,
    as_inputs,
    configured_path,
    load_eval_set,
)

pytestmark = pytest.mark.eval

_PATH = configured_path()


@pytest.mark.skipif(
    _PATH is None,
    reason=f"no scored set: set {EVAL_SET_PATH_ENV} to a path outside the repo",
)
def test_the_configured_scored_set_loads_and_says_how_much_is_marked():
    """Every row parses, and the marked counts are reported rather than asserted."""
    assert _PATH is not None  # narrowed for mypy; the skipif already decided
    rows = load_eval_set(_PATH)

    # Reported, not asserted. There is no floor to hold a partly-marked set to:
    # a set is marked over days, and the run_eval figures already exclude what
    # is unmarked. A count on the terminal is what tells the lead how far the
    # marking has got.
    print(
        f"\nrows={len(rows)} "
        f"typed={sum(row.note_type is not None for row in rows)} "
        f"vagueness={sum(row.is_vague is not None for row in rows)} "
        f"components={sum(row.missing_components is not None for row in rows)} "
        f"checks={sum(row.checks is not None for row in rows)}"
    )

    # The one thing worth failing on: a row that cannot be turned into the
    # inputs the three passes take is a row no run will ever reach.
    for ordinal, row in enumerate(rows, start=1):
        lead, note = as_inputs(row, ordinal)
        assert note.note == row.text
        assert lead.id == ordinal
