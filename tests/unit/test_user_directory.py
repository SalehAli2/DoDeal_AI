"""The user directory: the second backend ask, and the fake over it.

THE FIXTURE IS INVENTED. `tests/fixtures/user_directory/invented_users.json`
holds six people who do not exist, under tenant-a and tenant-b. A real
directory is a staff list, which is why the loader refuses a path inside the
repository -- demonstrated twice below, as the scored set's is.

WHAT IS PINNED HERE is the one thing nothing downstream can detect: a user_id
in the same space as a note's author_id. Nothing in a brief would look wrong if
the directory keyed on something else -- every figure would be right and every
name would be somebody else's -- so the contract is asserted as an exact field
set, the way the judgement row's is.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dodeal_ai.core.config import Settings
from dodeal_ai.core.errors import BriefStoreUnavailable
from dodeal_ai.units.structured_intelligence.eval_set import REPO_ROOT
from dodeal_ai.units.structured_intelligence.user_directory import (
    USER_DIRECTORY_PATH_ENV,
    FileUserDirectory,
    Role,
    User,
    UserDirectory,
    UserDirectoryError,
    configured_path,
    get_user_directory,
)

FIXTURE = Path("tests/fixtures/user_directory/invented_users.json")


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "user_id": 501,
        "name": "Idris Vale",
        "role": "rep",
        "team": "north",
    }
    entry.update(overrides)
    return entry


def _outside(tmp_path: Path, payload: object, name: str = "users.json") -> Path:
    assert not tmp_path.resolve().is_relative_to(REPO_ROOT)
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def directory(tmp_path: Path) -> FileUserDirectory:
    assert not tmp_path.resolve().is_relative_to(REPO_ROOT)
    destination = tmp_path / FIXTURE.name
    shutil.copyfile(FIXTURE, destination)
    return FileUserDirectory.from_path(destination)


# --- the contract -----------------------------------------------------------


def test_a_user_is_exactly_these_four_fields() -> None:
    """The ask, asserted exactly. No email, no phone, no manager id: this
    service sends nothing to anybody, so a contact detail here would be
    personal data held for no purpose at all."""
    assert set(User.model_fields) == {"user_id", "name", "role", "team"}


def test_there_are_three_roles_because_there_are_three_briefs() -> None:
    assert [role.value for role in Role] == ["rep", "team_leader", "head_of_sales"]


def test_a_head_of_sales_has_no_team() -> None:
    """Refused rather than tolerated: a head of sales with a team appears in
    that team's brief as though they were a member of it."""
    with pytest.raises(ValueError):
        User.model_validate(_entry(role="head_of_sales", team="north"))
    assert User.model_validate(_entry(role="head_of_sales", team=None)).team is None


@pytest.mark.parametrize("role", ["rep", "team_leader"])
def test_a_rep_and_a_leader_need_a_team(role: str) -> None:
    """A rep with no team appears in nobody's team brief -- silently."""
    with pytest.raises(ValueError):
        User.model_validate(_entry(role=role, team=None))


@pytest.mark.parametrize("value", [0, -1])
def test_a_user_id_is_at_least_one(value: int) -> None:
    with pytest.raises(ValueError):
        User.model_validate(_entry(user_id=value))


def test_an_unnamed_user_is_refused() -> None:
    """The directory exists to supply a name; an empty one is not one."""
    with pytest.raises(ValueError):
        User.model_validate(_entry(name=""))


def test_an_extra_field_is_refused() -> None:
    with pytest.raises(ValueError):
        User.model_validate(_entry(email="someone@example.com"))


def test_a_user_is_frozen() -> None:
    user = User.model_validate(_entry())
    with pytest.raises(ValueError):
        user.name = "Somebody Else"  # type: ignore[misc]


# --- the fake ---------------------------------------------------------------


def test_the_fake_is_a_user_directory(directory: FileUserDirectory) -> None:
    assert isinstance(directory, UserDirectory)


async def test_it_answers_one_tenants_people(directory: FileUserDirectory) -> None:
    users = await directory.users("tenant-a")
    assert [user.user_id for user in users] == [401, 402, 501, 502, 403, 503]
    assert {user.name for user in users if user.team == "north"} == {
        "Hana Reyes",
        "Idris Vale",
        "Noor Adeyemi",
    }


async def test_one_tenants_read_never_returns_anothers(
    directory: FileUserDirectory,
) -> None:
    users = await directory.users("tenant-b")
    assert [user.user_id for user in users] == [901]


async def test_an_unknown_tenant_answers_empty(directory: FileUserDirectory) -> None:
    """A state, not an error: the route turns it into the one refusal a caller
    can act on."""
    assert await directory.users("tenant-z") == []


# --- loading, and what is refused -------------------------------------------


def test_the_committed_fixture_cannot_be_loaded_where_it_lies() -> None:
    """The guard itself. A real directory is a staff list, and the easiest way
    for one to be committed is a loader that made it convenient."""
    with pytest.raises(UserDirectoryError, match="inside the repository"):
        FileUserDirectory.from_path(FIXTURE)


def test_a_missing_file_names_the_error_type_and_not_the_message(
    tmp_path: Path,
) -> None:
    with pytest.raises(UserDirectoryError, match="cannot read") as caught:
        FileUserDirectory.from_path(tmp_path / "absent.json")
    assert "FileNotFoundError" in str(caught.value)


def test_a_file_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(UserDirectoryError, match="not one JSON object"):
        FileUserDirectory.from_path(path)


def test_a_document_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UserDirectoryError, match="not one JSON object"):
        FileUserDirectory.from_path(_outside(tmp_path, [_entry()]))


def test_a_tenant_whose_value_is_not_a_list_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UserDirectoryError, match="tenant-a is not a list"):
        FileUserDirectory.from_path(_outside(tmp_path, {"tenant-a": _entry()}))


def test_a_bad_entry_names_its_position_and_never_a_name(tmp_path: Path) -> None:
    """An entry here is a real person by the time this matters, so the refusal
    says where it is and what kind of thing failed."""
    path = _outside(
        tmp_path, {"tenant-a": [_entry(), _entry(user_id=0, name="Wren Halloway")]}
    )
    with pytest.raises(UserDirectoryError) as caught:
        FileUserDirectory.from_path(path)
    message = str(caught.value)
    assert "tenant-a entry 2" in message
    assert "Wren Halloway" not in message


async def test_an_empty_document_loads_and_answers_empty(tmp_path: Path) -> None:
    loaded = FileUserDirectory.from_path(_outside(tmp_path, {}))
    assert await loaded.users("tenant-a") == []


# --- the path, and the provider ---------------------------------------------


def test_the_path_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(USER_DIRECTORY_PATH_ENV, raising=False)
    assert configured_path() is None


def test_a_blank_variable_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_DIRECTORY_PATH_ENV, "  ")
    assert configured_path() is None


def test_a_set_variable_is_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_DIRECTORY_PATH_ENV, "/somewhere/users.json")
    assert configured_path() == Path("/somewhere/users.json")


def test_it_is_not_a_settings_field() -> None:
    """A Settings field would reach .env.example and be copied into a
    deployment, which is the one place a fake staff list must never be."""
    assert "user_directory_path" not in Settings.model_fields


def test_no_directory_configured_is_a_503_and_never_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brief that fell back to printing ids would be forwarded to somebody
    who had to look every one of them up."""
    monkeypatch.delenv(USER_DIRECTORY_PATH_ENV, raising=False)
    get_user_directory.cache_clear()
    with pytest.raises(BriefStoreUnavailable) as caught:
        get_user_directory()
    assert caught.value.http_status == 503
    get_user_directory.cache_clear()


def test_the_provider_reads_the_file_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cached, so no request reads disk -- the rule core/tenant_config.py holds
    for tenant files."""
    path = _outside(tmp_path, {"tenant-a": [_entry()]})
    monkeypatch.setenv(USER_DIRECTORY_PATH_ENV, str(path))
    get_user_directory.cache_clear()
    try:
        first = get_user_directory()
        path.unlink()
        assert get_user_directory() is first
    finally:
        get_user_directory.cache_clear()
