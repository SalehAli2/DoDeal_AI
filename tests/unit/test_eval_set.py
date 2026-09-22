"""The scored-set format and its loader: what loads, what is refused, and how.

THE FIXTURE IS INVENTED. `tests/fixtures/eval_set/invented_notes.jsonl` holds
five notes written for this test and nothing from any real export -- that is the
rule the whole eval track exists under, and a fixture is the easiest place to
break it.

IT IS COPIED OUT OF THE REPOSITORY BEFORE IT IS LOADED. The loader refuses any
path inside the repo, so the committed fixture cannot be loaded where it lies.
That is not an awkwardness to work around: it is the guard, demonstrated twice
in this file -- once by copying the fixture to tmp_path and loading it, once by
pointing the loader at the fixture in place and watching it refuse.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dodeal_ai.units.structured_intelligence.eval_set import (
    EVAL_SET_PATH_ENV,
    REPO_ROOT,
    EvalRow,
    EvalSetError,
    as_inputs,
    configured_path,
    load_eval_set,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    CheckName,
    MissingComponent,
    NoteType,
)

FIXTURE = Path("tests/fixtures/eval_set/invented_notes.jsonl")
FIXTURE_ROWS = 5


def _outside(tmp_path: Path, text: str, name: str = "set.jsonl") -> Path:
    """Write `text` to a file outside the repository and return its path."""
    # The loader's refusal is only tested by the tests that mean to test it; a
    # tmp_path inside the repo would make every other test in this file fail for
    # the wrong reason, so it is asserted rather than assumed.
    assert not tmp_path.resolve().is_relative_to(REPO_ROOT)
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _fixture_outside(tmp_path: Path) -> Path:
    """The committed invented fixture, copied where the loader will read it."""
    assert not tmp_path.resolve().is_relative_to(REPO_ROOT)
    destination = tmp_path / FIXTURE.name
    shutil.copyfile(FIXTURE, destination)
    return destination


def test_repo_root_really_is_the_repository_root():
    """REPO_ROOT points at the checkout, so the refusal below covers the repo."""
    assert (REPO_ROOT / "pyproject.toml").is_file()
    assert (REPO_ROOT / "src" / "dodeal_ai").is_dir()


def test_a_valid_row_loads_with_every_mark_it_carries(tmp_path):
    """A fully marked row comes back with its type, vagueness and checks."""
    rows = load_eval_set(_fixture_outside(tmp_path))
    assert len(rows) == FIXTURE_ROWS

    first = rows[0]
    assert first.id == "inv-01"
    assert first.note_type is NoteType.DISCOVERY
    assert first.is_vague is True
    assert first.missing_components == [MissingComponent.NEXT_STEP_WITH_DATE]
    assert first.checks is not None
    assert first.checks[CheckName.WH_OUTCOME] is True
    assert first.checks[CheckName.CS_OWN_TERMS] is False
    assert first.lead.enquiryType == "buy"


def test_an_absent_expected_field_loads_as_none_and_is_never_defaulted(tmp_path):
    """An unmarked field is None, not False and not an empty list."""
    rows = {row.id: row for row in load_eval_set(_fixture_outside(tmp_path))}

    # Marked for the first two passes, not yet scored.
    partial = rows["inv-03"]
    assert partial.note_type is NoteType.VIEWING
    assert partial.is_vague is True
    assert partial.checks is None

    # Marked for nothing at all: every expected field absent.
    unmarked = rows["inv-04"]
    assert unmarked.note_type is None
    assert unmarked.is_vague is None
    assert unmarked.missing_components is None
    assert unmarked.checks is None


def test_an_empty_missing_components_list_is_a_mark_and_not_an_absence(tmp_path):
    """ "Nothing is missing" is an answer, and it survives as an empty list."""
    rows = {row.id: row for row in load_eval_set(_fixture_outside(tmp_path))}
    not_vague = rows["inv-05"]
    assert not_vague.is_vague is False
    assert not_vague.missing_components == []


def test_a_malformed_row_is_refused_by_line_number_and_never_by_text(tmp_path):
    """A row that is not a row names its line, its field and the error type."""
    note = "a note nobody may ever read in a failure message"
    lines = [
        json.dumps({"id": "ok-1", "text": note, "lead": {}}),
        json.dumps({"id": "bad", "text": note, "lead": {}, "is_vague": "yes"}),
    ]
    with pytest.raises(EvalSetError) as caught:
        load_eval_set(_outside(tmp_path, "\n".join(lines) + "\n"))

    message = str(caught.value)
    assert "line 2" in message
    assert "is_vague" in message
    assert note not in message


def test_a_line_that_is_not_json_is_refused_by_line_number(tmp_path):
    """A line the decoder cannot read names its line and not its content."""
    secret = "not json at all"
    body = json.dumps({"id": "ok-1", "text": "a perfectly fine note", "lead": {}})
    with pytest.raises(EvalSetError) as caught:
        load_eval_set(_outside(tmp_path, f"{body}\n{secret}\n"))

    assert "line 2" in str(caught.value)
    assert secret not in str(caught.value)


def test_a_band_or_a_total_in_a_row_is_refused(tmp_path):
    """The one number the unit computes may never be supplied by an input."""
    for field in ("band", "total", "score"):
        row = {"id": "x", "text": "a perfectly fine note", "lead": {}, field: "good"}
        with pytest.raises(EvalSetError) as caught:
            load_eval_set(_outside(tmp_path, json.dumps(row) + "\n"))
        assert "extra_forbidden" in str(caught.value)


def test_the_row_model_has_no_band_or_total_field():
    """Asserted by introspection, so a future field cannot slip past the file."""
    forbidden = {"band", "total", "score", "mark", "marks"}
    assert not forbidden & set(EvalRow.model_fields)


def test_a_repeated_id_is_refused(tmp_path):
    """Two rows with one id would make a diagnostics row unidentifiable."""
    row = {"id": "same", "text": "a perfectly fine note", "lead": {}}
    body = json.dumps(row)
    with pytest.raises(EvalSetError) as caught:
        load_eval_set(_outside(tmp_path, f"{body}\n{body}\n"))
    assert "line 2" in str(caught.value)
    assert "repeated id" in str(caught.value)


def test_blank_lines_are_skipped_and_do_not_shift_the_line_number(tmp_path):
    """A hand-edited file ends with a blank line, and the count still holds."""
    body = json.dumps({"id": "one", "text": "a perfectly fine note", "lead": {}})
    bad = json.dumps({"id": "two", "text": "another fine note", "lead": {}, "x": 1})
    rows = load_eval_set(_outside(tmp_path, f"\n{body}\n\n"))
    assert len(rows) == 1

    with pytest.raises(EvalSetError) as caught:
        load_eval_set(_outside(tmp_path, f"\n{body}\n\n{bad}\n", name="b.jsonl"))
    assert "line 4" in str(caught.value)


def test_a_file_with_no_rows_is_refused(tmp_path):
    """An empty set would report 100 % agreement over nothing."""
    with pytest.raises(EvalSetError, match="no rows"):
        load_eval_set(_outside(tmp_path, "\n\n"))


def test_a_file_that_cannot_be_read_is_refused_by_type_and_not_by_message(tmp_path):
    """A missing file names the path and the error type, never the OS message."""
    with pytest.raises(EvalSetError) as caught:
        load_eval_set(tmp_path / "does-not-exist.jsonl")
    assert "FileNotFoundError" in str(caught.value)


def test_a_path_inside_the_repository_is_refused():
    """Real notes never enter this repo, and the loader is what enforces it."""
    with pytest.raises(EvalSetError, match="inside the repository"):
        load_eval_set(FIXTURE)


def test_a_path_inside_the_repository_is_refused_through_a_relative_path(tmp_path):
    """The refusal resolves first, so `./` and `..` cannot walk back in."""
    walked = tmp_path / ".." / tmp_path.name
    assert load_eval_set(_fixture_outside(tmp_path)) == load_eval_set(
        walked / FIXTURE.name
    )
    with pytest.raises(EvalSetError, match="inside the repository"):
        load_eval_set(REPO_ROOT / "src" / ".." / "tests" / "eval-set.jsonl")


def test_an_unset_variable_reads_as_no_path(monkeypatch):
    """Unset means the eval tests skip, so it is None and never a guess."""
    monkeypatch.delenv(EVAL_SET_PATH_ENV, raising=False)
    assert configured_path() is None


def test_a_blank_variable_reads_as_unset(monkeypatch):
    """An exported-but-empty variable is a shell saying it did not set this."""
    monkeypatch.setenv(EVAL_SET_PATH_ENV, "   ")
    assert configured_path() is None


def test_a_set_variable_reads_as_that_path(monkeypatch, tmp_path):
    """The path is taken from the environment and never from Settings."""
    monkeypatch.setenv(EVAL_SET_PATH_ENV, str(tmp_path / "set.jsonl"))
    assert configured_path() == tmp_path / "set.jsonl"


def test_a_row_becomes_the_lead_and_note_the_three_passes_take(tmp_path):
    """One row, one Lead and one LeadNote, so both scripts send the same bytes."""
    rows = load_eval_set(_fixture_outside(tmp_path))
    lead, note = as_inputs(rows[0], 7)

    assert lead.id == 7
    assert lead.enquiryType == "buy"
    assert lead.project == "Project Alpha"
    assert note.id == 7
    assert note.note == rows[0].text
    assert note.createdAt.tzinfo is not None
