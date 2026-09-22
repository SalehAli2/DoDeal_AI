"""The measures routes (register item 144): the three measures as data, per rep
and per team, behind the service chain, the rep-numbers switch and the
deadline; a measure under the floor is a state and never a 0."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.api.routes import judgements as judgement_routes
from dodeal_ai.core.config import get_settings
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    FileJudgementStore,
    JudgementRow,
    get_judgement_store,
)
from dodeal_ai.units.structured_intelligence.user_directory import (
    FileUserDirectory,
    Role,
    User,
    get_user_directory,
)
from tests.helpers import tokens

REPS = "/api/v1/measures/reps"
TEAMS = "/api/v1/measures/teams"

IDRIS = User(user_id=501, name="Idris Vale", role=Role.REP, team="north")
NOOR = User(user_id=502, name="Noor Adeyemi", role=Role.REP, team="north")
PEOPLE = [IDRIS, NOOR]
OPTED_IN = dataclasses.replace(get_tenant_config("tenant-a"), rep_numbers_enabled=True)


def _row(author_id: int, note_id: int, total: int = 72, flagged: bool = False):
    return JudgementRow.model_validate(
        {
            "note_id": note_id,
            "lead_id": 9000 + note_id,
            "author_id": author_id,
            "note_created_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
            "note_type": "discovery",
            "band": OPTED_IN.band_for(total).value,
            "total": total,
            "denominator": 80,
            "suppressed_reason": None,
            "prompt_sent": False,
            "enforcement_verdict": "flag" if flagged else "allow",
            "rubric_version": "note_rubric_v2",
            "prompt_version": "unit_a_prompts_v3",
            "model_version": "invented-model-1",
            "config_version": "tenant-cfg-default-4",
        }
    )


@pytest.fixture
def store() -> FileJudgementStore:
    rows = [_row(501, n, flagged=n < 103) for n in range(100, 112)]
    rows += [_row(502, n, total=40) for n in range(200, 204)]
    return FileJudgementStore({"tenant-a": rows})


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()

    async def _opted_in(_tenant: str):
        return OPTED_IN

    monkeypatch.setattr(judgement_routes, "resolve_tenant_config", _opted_in)
    app.dependency_overrides[get_judgement_store] = lambda: store
    app.dependency_overrides[get_user_directory] = lambda: FileUserDirectory(
        {"tenant-a": PEOPLE}
    )
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }


def test_a_rep_gets_three_measures_the_average_total_and_the_window(client) -> None:
    """value, state, n, floor, excluded for each; the mean total; the window."""
    body = client.get(f"{REPS}/501", headers=_headers()).json()
    assert (body["author_id"], body["name"]) == (501, "Idris Vale")
    assert body["measures"]["average_band"] == {
        "value": "good",
        "state": "reported",
        "n": 12,
        "floor": 10,
        "excluded": 0,
    }
    assert body["measures"]["flagged_share"]["value"] == 25
    assert body["measures"]["improved_share"]["state"] == "nothing_to_measure"
    assert body["average_total"] == 72
    assert set(body["window"]) == {"since", "until", "first_day", "last_day", "days"}


def test_a_floor_suppressed_measure_is_a_state_never_a_zero(client) -> None:
    """Four notes under a floor of ten: null values, the state, and the count."""
    body = client.get(f"{REPS}/502", headers=_headers()).json()
    band = body["measures"]["average_band"]
    assert (band["value"], band["state"], band["n"]) == (
        None,
        "below_evidence_floor",
        4,
    )
    flagged = body["measures"]["flagged_share"]
    assert (flagged["value"], flagged["state"]) == (None, "below_evidence_floor")
    assert body["average_total"] is None


def test_a_team_is_pooled_and_then_each_member(client) -> None:
    """Sixteen notes pooled; each member's own figures beneath, by name."""
    body = client.get(f"{TEAMS}/north", headers=_headers()).json()
    assert body["team"] == "north"
    assert body["measures"]["average_band"]["n"] == 16
    assert [member["name"] for member in body["members"]] == [
        "Idris Vale",
        "Noor Adeyemi",
    ]
    assert body["members"][1]["measures"]["average_band"]["state"] == (
        "below_evidence_floor"
    )


@pytest.mark.parametrize("path", [f"{REPS}/999", f"{TEAMS}/west"])
def test_an_unknown_author_or_team_is_404(client, path) -> None:
    r = client.get(path, headers=_headers())
    assert (r.status_code, r.json()["reason"]) == (404, "subject_not_found")


@pytest.mark.parametrize("path", [f"{REPS}/501", f"{TEAMS}/north"])
def test_a_tenant_that_has_not_opted_in_is_403(client, monkeypatch, path) -> None:
    async def _default(tenant: str):
        return get_tenant_config(tenant)

    monkeypatch.setattr(judgement_routes, "resolve_tenant_config", _default)
    r = client.get(path, headers=_headers())
    assert (r.status_code, r.json()["reason"]) == (403, "rep_numbers_not_enabled")


@pytest.mark.parametrize("path", [f"{REPS}/501", f"{TEAMS}/north"])
def test_a_slow_store_is_503_at_the_deadline(client, monkeypatch, store, path) -> None:
    monkeypatch.setenv("DODEAL_JUDGEMENT_DEADLINE_SECONDS", "0.05")
    get_settings.cache_clear()

    async def _slow(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(store, "rows_between", _slow)
    r = client.get(path, headers=_headers())
    assert (r.status_code, r.json()["reason"]) == (503, "brief_deadline_exceeded")


def test_a_user_token_is_401(client) -> None:
    user = {
        "Authorization": f"Bearer {tokens.mint_token(subdomain='tenant-a', sub=42)}",
        "Host": "tenant-a.dodealcrm.com",
    }
    assert client.get(f"{REPS}/501", headers=user).status_code == 401
