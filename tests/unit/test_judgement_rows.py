"""The judgement-row contract and the fake store over it.

THE FIXTURE IS INVENTED. `tests/fixtures/judgement_rows/invented_rows.json`
holds six rows written for this test: invented ids, invented tenants, an
invented model stamp. Nothing in it came from any export, and a row carries no
note text at all -- which is the contract's first rule and the easiest one for
a fixture to break.

IT IS COPIED OUT OF THE REPOSITORY BEFORE IT IS LOADED, the same guard the
scored set has and for the same reason: a real export of these rows names real
leads and real salespeople. Demonstrated twice below -- once by copying the
fixture to tmp_path and loading it, once by pointing the loader at the fixture
where it lies and watching it refuse.

THE FIELD-SET TEST IS THE CONTRACT TEST. This model is the attachment for a
backend ask, so a field added or renamed after the ask is a field changed in
someone else's schema. The set is asserted exactly, not with `in`.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from dodeal_ai.core.config import Settings
from dodeal_ai.units.structured_intelligence.eval_set import REPO_ROOT
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    JUDGEMENT_ROWS_PATH_ENV,
    FileJudgementStore,
    JudgementRow,
    JudgementStore,
    JudgementStoreError,
    configured_path,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    Band,
    EnforcementVerdict,
    SuppressedReason,
)

FIXTURE = Path("tests/fixtures/judgement_rows/invented_rows.json")

CAIRO = timezone(timedelta(hours=3))
DAY_ONE = datetime(2026, 9, 1, tzinfo=CAIRO)
DAY_TWO = datetime(2026, 9, 2, tzinfo=CAIRO)
DAY_THREE = datetime(2026, 9, 3, tzinfo=CAIRO)


def _scored(**overrides: object) -> dict[str, object]:
    """A complete scored row as a dict, for the validation tests to bend."""
    row: dict[str, object] = {
        "note_id": 1,
        "lead_id": 2,
        "author_id": 3,
        "note_created_at": "2026-09-01T09:00:00+03:00",
        "note_type": "discovery",
        "band": "good",
        "total": 72,
        "denominator": 80,
        "suppressed_reason": None,
        "prompt_sent": False,
        "enforcement_verdict": "allow",
        "rubric_version": "note_rubric_v2",
        "prompt_version": "unit_a_prompts_v2",
        "model_version": "invented-model-1",
        "config_version": "tenant-cfg-default-4",
    }
    row.update(overrides)
    return row


def _outside(tmp_path: Path, payload: object, name: str = "rows.json") -> Path:
    """Write `payload` as JSON outside the repository and return its path."""
    # Asserted rather than assumed: a tmp_path inside the repo would make every
    # test here fail for the loader's reason instead of its own.
    assert not tmp_path.resolve().is_relative_to(REPO_ROOT)
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixture_outside(tmp_path: Path) -> Path:
    """The committed invented fixture, copied where the loader will read it."""
    assert not tmp_path.resolve().is_relative_to(REPO_ROOT)
    destination = tmp_path / FIXTURE.name
    shutil.copyfile(FIXTURE, destination)
    return destination


@pytest.fixture
def store(tmp_path: Path) -> FileJudgementStore:
    return FileJudgementStore.from_path(_fixture_outside(tmp_path))


# --- the contract -----------------------------------------------------------


def test_the_row_carries_exactly_these_fields_and_no_note_text() -> None:
    """The backend ask, asserted exactly: a field added here is a field somebody
    else has to add to their schema, so it may not arrive by accident."""
    assert set(JudgementRow.model_fields) == {
        "note_id",
        "lead_id",
        "author_id",
        "note_created_at",
        "note_type",
        "band",
        "total",
        "denominator",
        "suppressed_reason",
        "prompt_sent",
        "enforcement_verdict",
        "rubric_version",
        "prompt_version",
        "model_version",
        "config_version",
    }


@pytest.mark.parametrize("name", ["note", "note_text", "text", "body", "content"])
def test_no_note_body_may_arrive_under_any_name(name: str) -> None:
    """We do not hold note bodies. extra="forbid" is what makes that structural
    rather than a promise nobody checks."""
    with pytest.raises(ValueError):
        JudgementRow.model_validate(_scored(**{name: "Called the client."}))


def test_a_row_is_frozen() -> None:
    """A measure reads these; it must not be able to change one under another."""
    row = JudgementRow.model_validate(_scored())
    with pytest.raises(ValueError):
        row.band = Band.POOR  # type: ignore[misc]


# --- scored or suppressed, never both and never neither ---------------------


def test_a_scored_row_validates() -> None:
    row = JudgementRow.model_validate(_scored())
    assert row.band is Band.GOOD
    assert (row.total, row.denominator) == (72, 80)
    assert row.suppressed_reason is None
    assert row.enforcement_verdict is EnforcementVerdict.ALLOW


def test_a_suppressed_row_validates() -> None:
    row = JudgementRow.model_validate(
        _scored(
            note_type=None,
            band=None,
            total=None,
            denominator=None,
            suppressed_reason="insufficient_evidence",
            model_version="",
        )
    )
    assert row.suppressed_reason is SuppressedReason.INSUFFICIENT_EVIDENCE
    assert (row.band, row.total, row.denominator) == (None, None, None)
    # The one stamp that may be empty: no model ran, so nothing reported one.
    assert row.model_version == ""


def test_a_row_that_is_both_scored_and_suppressed_is_refused() -> None:
    """A band beside a suppression is the shape that would be averaged into a
    measure as if somebody had judged it."""
    with pytest.raises(ValueError):
        JudgementRow.model_validate(_scored(suppressed_reason="not_scorable"))


@pytest.mark.parametrize("missing", ["band", "total", "denominator"])
def test_a_row_that_is_neither_is_refused(missing: str) -> None:
    """Half a score is not a score: dropping any one of the three leaves a row
    nobody can read as scored or as suppressed."""
    with pytest.raises(ValueError):
        JudgementRow.model_validate(_scored(**{missing: None}))


@pytest.mark.parametrize("field", ["note_id", "lead_id", "author_id"])
@pytest.mark.parametrize("value", [0, -1])
def test_every_id_is_at_least_one(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        JudgementRow.model_validate(_scored(**{field: value}))


def test_a_naive_note_timestamp_is_read_as_utc() -> None:
    """ASSUMPTION[Q5], the same rule LeadNote applies to the same instant."""
    row = JudgementRow.model_validate(_scored(note_created_at="2026-09-01T09:00:00"))
    assert row.note_created_at == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def test_an_offset_timestamp_keeps_its_offset() -> None:
    row = JudgementRow.model_validate(_scored())
    assert row.note_created_at == datetime(2026, 9, 1, 6, 0, tzinfo=UTC)


# --- the fake store ---------------------------------------------------------


def test_the_fake_is_a_judgement_store(store: FileJudgementStore) -> None:
    """Conformance asserted the way core/llm/client.py's is."""
    assert isinstance(store, JudgementStore)


async def test_a_tenants_rows_come_back_in_the_order_they_were_recorded(
    store: FileJudgementStore,
) -> None:
    """Item 144 has nothing else to order by: the two rows for note 1002 carry
    the same note timestamp, so only their position says which came second."""
    rows = await store.rows_between("tenant-a", since=DAY_ONE, until=DAY_THREE)
    assert [row.note_id for row in rows] == [1001, 1002, 1002, 1003, 1004]
    first, second = (row for row in rows if row.note_id == 1002)
    assert (first.band, second.band) == (Band.FAIR, Band.GOOD)


async def test_the_window_is_half_open(store: FileJudgementStore) -> None:
    """[since, until): day one's window ends where day two's begins, and the
    08:15 row on day two belongs to exactly one of them."""
    day_one = await store.rows_between("tenant-a", since=DAY_ONE, until=DAY_TWO)
    day_two = await store.rows_between("tenant-a", since=DAY_TWO, until=DAY_THREE)
    assert [row.note_id for row in day_one] == [1001, 1002, 1002]
    assert [row.note_id for row in day_two] == [1003, 1004]


async def test_a_row_exactly_on_since_is_included(store: FileJudgementStore) -> None:
    """The lower bound is inclusive, so nothing falls between two tiled windows."""
    at_0815 = datetime(2026, 9, 2, 8, 15, tzinfo=CAIRO)
    rows = await store.rows_between("tenant-a", since=at_0815, until=DAY_THREE)
    assert [row.note_id for row in rows] == [1003, 1004]


async def test_an_author_narrows_the_read(store: FileJudgementStore) -> None:
    rows = await store.rows_between(
        "tenant-a", since=DAY_ONE, until=DAY_THREE, author_id=502
    )
    assert {row.author_id for row in rows} == {502}
    assert [row.note_id for row in rows] == [1003, 1004]


async def test_an_author_with_nothing_in_the_window_answers_empty(
    store: FileJudgementStore,
) -> None:
    """Empty is an answer, not an error: the measures suppress on it for want
    of evidence, which is the right outcome."""
    rows = await store.rows_between(
        "tenant-a", since=DAY_ONE, until=DAY_THREE, author_id=999
    )
    assert rows == []


async def test_one_tenants_read_never_returns_anothers(
    store: FileJudgementStore,
) -> None:
    """The tenant is the container, so a read cannot cross one by forgetting a
    filter -- there is no filter to forget."""
    rows = await store.rows_between("tenant-a", since=DAY_ONE, until=DAY_THREE)
    assert 9001 not in {row.note_id for row in rows}
    other = await store.rows_between("tenant-b", since=DAY_ONE, until=DAY_THREE)
    assert [row.note_id for row in other] == [9001]


async def test_an_unknown_tenant_answers_empty(store: FileJudgementStore) -> None:
    assert await store.rows_between("tenant-z", since=DAY_ONE, until=DAY_THREE) == []


async def test_a_reversed_window_is_refused(store: FileJudgementStore) -> None:
    """Not answered empty: an empty list reads as "this rep wrote nothing",
    which a measure reports as no evidence rather than as a bug."""
    with pytest.raises(JudgementStoreError):
        await store.rows_between("tenant-a", since=DAY_THREE, until=DAY_ONE)


@pytest.mark.parametrize(
    ("since", "until"),
    [
        (DAY_ONE.replace(tzinfo=None), DAY_THREE),
        (DAY_ONE, DAY_THREE.replace(tzinfo=None)),
    ],
)
async def test_a_naive_bound_is_refused(
    store: FileJudgementStore, since: datetime, until: datetime
) -> None:
    """Refused rather than coerced: a naive bound is somebody's local midnight,
    and guessing which would move a whole day of a rep's work."""
    with pytest.raises(JudgementStoreError):
        await store.rows_between("tenant-a", since=since, until=until)


# --- loading, and what is refused -------------------------------------------


def test_the_committed_fixture_cannot_be_loaded_where_it_lies() -> None:
    """The guard itself: a path inside the repository is refused before the
    file is opened, so a real export cannot be made convenient to commit."""
    with pytest.raises(JudgementStoreError, match="inside the repository"):
        FileJudgementStore.from_path(FIXTURE)


def test_a_missing_file_names_the_error_type_and_not_the_message(
    tmp_path: Path,
) -> None:
    with pytest.raises(JudgementStoreError, match="cannot read") as caught:
        FileJudgementStore.from_path(tmp_path / "absent.json")
    assert "FileNotFoundError" in str(caught.value)


def test_a_file_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "rows.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(JudgementStoreError, match="not one JSON object"):
        FileJudgementStore.from_path(path)


def test_a_document_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    """A bare list has no tenant, and reading one as a tenant's rows is how a
    store starts answering one tenant with another's data."""
    with pytest.raises(JudgementStoreError, match="not one JSON object"):
        FileJudgementStore.from_path(_outside(tmp_path, [_scored()]))


def test_a_tenant_whose_value_is_not_a_list_is_refused(tmp_path: Path) -> None:
    with pytest.raises(JudgementStoreError, match="tenant-a is not a list"):
        FileJudgementStore.from_path(_outside(tmp_path, {"tenant-a": _scored()}))


def test_a_bad_row_names_its_position_and_never_its_contents(
    tmp_path: Path,
) -> None:
    """The row holds a real lead's id and a real person's id by the time this
    matters, so the refusal says where it is and what kind of thing failed."""
    path = _outside(
        tmp_path, {"tenant-a": [_scored(), _scored(note_id=0, lead_id=4242)]}
    )
    with pytest.raises(JudgementStoreError) as caught:
        FileJudgementStore.from_path(path)
    message = str(caught.value)
    assert "tenant-a row 2" in message
    assert "4242" not in message


def test_a_row_with_a_note_body_is_refused_by_the_loader(tmp_path: Path) -> None:
    """The contract's first rule, enforced where a file reaches it and not only
    where a caller does."""
    path = _outside(tmp_path, {"tenant-a": [_scored(note_text="Called them.")]})
    with pytest.raises(JudgementStoreError, match="tenant-a row 1"):
        FileJudgementStore.from_path(path)


async def test_an_empty_document_loads_and_answers_empty(tmp_path: Path) -> None:
    """A store with no rows yet is an ordinary state, not a broken file."""
    loaded = FileJudgementStore.from_path(_outside(tmp_path, {}))
    assert await loaded.rows_between("tenant-a", since=DAY_ONE, until=DAY_THREE) == []


# --- the path comes from the environment ------------------------------------


def test_the_path_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JUDGEMENT_ROWS_PATH_ENV, raising=False)
    assert configured_path() is None


def test_a_blank_variable_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty variable is how a shell says "I did not set this"."""
    monkeypatch.setenv(JUDGEMENT_ROWS_PATH_ENV, "   ")
    assert configured_path() is None


def test_a_set_variable_is_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGEMENT_ROWS_PATH_ENV, "/somewhere/rows.json")
    assert configured_path() == Path("/somewhere/rows.json")


def test_it_is_not_a_settings_field() -> None:
    """The reason it is an environment variable: a Settings field would reach
    .env.example, be copied into a deployment, and let the service be pointed
    at a file of invented judgements."""
    assert "judgement_rows_path" not in Settings.model_fields
