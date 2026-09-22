"""The brief's two file fakes load in the lifespan (register item 155): a bad or
in-repo file refuses startup naming the store and never the path, and a brief
after startup reads no file."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    JUDGEMENT_ROWS_PATH_ENV,
)
from dodeal_ai.units.structured_intelligence.user_directory import (
    USER_DIRECTORY_PATH_ENV,
)
from tests.helpers import tokens

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_ROWS = _FIXTURES / "judgement_rows" / "invented_rows.json"
_USERS = _FIXTURES / "user_directory" / "invented_users.json"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Both files copied OUTSIDE the repository, their variables set."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    rows = tmp_path / "rows.json"
    users = tmp_path / "users.json"
    shutil.copyfile(_ROWS, rows)
    shutil.copyfile(_USERS, users)
    monkeypatch.setenv(JUDGEMENT_ROWS_PATH_ENV, str(rows))
    monkeypatch.setenv(USER_DIRECTORY_PATH_ENV, str(users))
    get_settings.cache_clear()
    yield {"rows": rows, "users": users}
    get_settings.cache_clear()


def _refusal(monkeypatch: pytest.MonkeyPatch) -> str:
    with pytest.raises(ConfigError) as caught, TestClient(app):
        pass
    assert caught.value.__cause__ is None
    return str(caught.value)


@pytest.mark.parametrize(
    ("which", "code"),
    [("rows", "judgement_store_invalid"), ("users", "user_directory_invalid")],
)
def test_a_malformed_file_refuses_startup_naming_the_store(
    env, monkeypatch, which, code
) -> None:
    """Not JSON: startup refuses with the store's code and no path."""
    env[which].write_text("{not json", encoding="utf-8")
    message = _refusal(monkeypatch)
    assert message == code
    assert str(env[which]) not in message


@pytest.mark.parametrize(
    ("variable", "code", "inside"),
    [
        (JUDGEMENT_ROWS_PATH_ENV, "judgement_store_invalid", _ROWS),
        (USER_DIRECTORY_PATH_ENV, "user_directory_invalid", _USERS),
    ],
)
def test_a_file_inside_the_repository_refuses_startup(
    env, monkeypatch, variable, code, inside
) -> None:
    """The committed fixtures themselves are refused where they lie."""
    monkeypatch.setenv(variable, str(inside))
    assert _refusal(monkeypatch) == code


def test_a_brief_after_startup_reads_no_file(env, monkeypatch) -> None:
    """Both files deleted and every read refused: the brief still answers."""
    with TestClient(app) as client:
        env["rows"].unlink()
        env["users"].unlink()

        def _no_reads(*_args, **_kwargs):
            raise AssertionError("a brief read a file")

        monkeypatch.setattr(Path, "read_text", _no_reads)
        headers = {
            "Authorization": f"Bearer {tokens.mint_service_token()}",
            "Host": "tenant-a.dodealcrm.com",
        }
        response = client.get("/api/v1/briefs/head_of_sales/401", headers=headers)
    assert response.status_code in (200, 204)


def test_with_no_paths_set_nothing_is_loaded(monkeypatch) -> None:
    """Unset is the ordinary state: startup succeeds and the brief is 503."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    monkeypatch.delenv(JUDGEMENT_ROWS_PATH_ENV, raising=False)
    monkeypatch.delenv(USER_DIRECTORY_PATH_ENV, raising=False)
    get_settings.cache_clear()
    with TestClient(app) as client:
        headers = {
            "Authorization": f"Bearer {tokens.mint_service_token()}",
            "Host": "tenant-a.dodealcrm.com",
        }
        response = client.get("/api/v1/briefs/rep/501", headers=headers)
    assert response.json()["reason"] == "brief_store_unavailable"
    get_settings.cache_clear()
