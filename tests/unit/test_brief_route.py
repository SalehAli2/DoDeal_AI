"""The brief route over HTTP: the gates still hold, and the four answers.

FOUR ANSWERS, and the point of the piece is that they are four and not two:

  200  there is something to say, as text/plain.
  204  every measure was suppressed. THE BRIEF IS NOT SENT -- an empty daily
       email trains people to ignore the channel within a fortnight.
  404  the directory does not list this subject. NOT a 204: a CRM sending a
       wrong id would otherwise read a quiet day, every day, for ever.
  503  no store or no directory is configured, which is every deployment
       today. NOT a 204, and never an empty brief: it would tell a manager
       they had a quiet month when nobody had wired the store up.

The store and the directory are the fakes from items 143 and 145, built in
memory from invented rows and invented people and injected through
`app.dependency_overrides`, the same way every other seam in this suite is.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import dodeal_ai.api.routes.judgements as judgement_routes
import dodeal_ai.core.cost.limiter as cost_limiter
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
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
from tests.helpers.fake_cost_redis import FakeCostRedis

BRIEFS = "/api/v1/briefs"

HEAD = User(user_id=401, name="Ayla Brook", role=Role.HEAD_OF_SALES, team=None)
LEADER = User(user_id=402, name="Hana Reyes", role=Role.TEAM_LEADER, team="north")
IDRIS = User(user_id=501, name="Idris Vale", role=Role.REP, team="north")
NOOR = User(user_id=502, name="Noor Adeyemi", role=Role.REP, team="north")
PEOPLE = [HEAD, LEADER, IDRIS, NOOR]


def _row(author_id: int, note_id: int, *, flagged: bool = False) -> JudgementRow:
    """One invented row, dated inside any rolling window the route can build:
    the window is whole local days ending at local midnight today (register item
    156), so `now` minus one day is always in."""
    return JudgementRow.model_validate(
        {
            "note_id": note_id,
            "lead_id": 9000 + note_id,
            "author_id": author_id,
            "note_created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "note_type": "discovery",
            "band": "good",
            "total": 72,
            "denominator": 80,
            "suppressed_reason": None,
            "prompt_sent": False,
            "enforcement_verdict": "flag" if flagged else "allow",
            "rubric_version": "note_rubric_v2",
            "prompt_version": "unit_a_prompts_v2",
            "model_version": "invented-model-1",
            "config_version": "tenant-cfg-default-4",
        }
    )


def _rows_for(author_id: int, count: int, first_note: int) -> list[JudgementRow]:
    return [_row(author_id, first_note + index) for index in range(count)]


@pytest.fixture
def rows() -> list[JudgementRow]:
    """Twelve notes each for two reps: enough to clear the shipped floor of 10
    without touching the tenant's config."""
    return _rows_for(501, 12, 100) + _rows_for(502, 12, 200)


@pytest.fixture
def store(rows: list[JudgementRow]) -> FileJudgementStore:
    return FileJudgementStore({"tenant-a": rows})


@pytest.fixture
def directory() -> FileUserDirectory:
    return FileUserDirectory({"tenant-a": PEOPLE})


@pytest.fixture
def client(monkeypatch, store, directory):
    """The gate-chain wiring, plus the two seams this route reads."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )
    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: FakeCostRedis())

    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    app.dependency_overrides[get_judgement_store] = lambda: store
    app.dependency_overrides[get_user_directory] = lambda: directory
    # Register item 154: a tenant opts in to per-person figures; these tests
    # are about the brief, so their tenant has.
    monkeypatch.setattr(judgement_routes, "resolve_tenant_config", _opted_in)

    yield TestClient(app)

    app.dependency_overrides.clear()
    get_settings.cache_clear()


OPTED_IN = dataclasses.replace(get_tenant_config("tenant-a"), rep_numbers_enabled=True)


async def _opted_in(_tenant: str):
    return OPTED_IN


def _headers(subdomain: str = "tenant-a") -> dict:
    """The CRM's service token: the brief route's only credential (item 153)."""
    return {
        "Authorization": f"Bearer {tokens.mint_service_token(subdomain=subdomain)}",
        "Host": f"{subdomain}.dodealcrm.com",
    }


# --- the gates still hold ---------------------------------------------------


def test_no_token_is_401(client) -> None:
    r = client.get(f"{BRIEFS}/rep/501", headers={"Host": "tenant-a.dodealcrm.com"})
    assert r.status_code == 401


def test_a_user_token_is_401(client) -> None:
    """Register item 153: a person's token cannot read anyone's brief."""
    user = {
        "Authorization": f"Bearer {tokens.mint_token(subdomain='tenant-a', sub=42)}",
        "Host": "tenant-a.dodealcrm.com",
    }
    assert client.get(f"{BRIEFS}/rep/501", headers=user).status_code == 401


def test_a_token_for_another_tenant_is_403(client) -> None:
    """Gate 2, on this route as on every other: the brief is a whole tenant's
    performance data, and it is the one thing here nobody may cross."""
    r = client.get(
        f"{BRIEFS}/rep/501",
        headers={**_headers(), "Host": "tenant-b.dodealcrm.com"},
    )
    assert r.status_code == 403


def test_a_tenant_with_no_rows_of_its_own_gets_nothing(client) -> None:
    """The store is keyed by tenant, so tenant-b cannot see tenant-a's rows --
    there is no filter to forget."""
    r = client.get(f"{BRIEFS}/rep/501", headers=_headers("tenant-b"))
    # 404 before 204: tenant-b's directory is empty, so the subject is unknown.
    assert r.status_code == 404
    assert r.json()["reason"] == "subject_not_found"


# --- the four answers -------------------------------------------------------


def test_a_rep_brief_is_json_carrying_its_text(client) -> None:
    """Register item 154: JSON, with the text a person reads inside it."""
    r = client.get(f"{BRIEFS}/rep/501", headers=_headers())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["text"].startswith("Note quality for Idris Vale.\n")
    assert "over 12 notes" in r.json()["text"]


def test_a_team_brief_names_the_team_and_its_people(client) -> None:
    r = client.get(f"{BRIEFS}/team_leader/402", headers=_headers())
    assert r.status_code == 200
    text = r.json()["text"]
    assert "Hana Reyes's team (north)" in text
    assert "Idris Vale" in text and "Noor Adeyemi" in text
    assert "over 24 notes" in text


def test_an_org_brief_covers_every_team(client) -> None:
    r = client.get(f"{BRIEFS}/head_of_sales/401", headers=_headers())
    assert r.status_code == 200
    assert "across all teams, for Ayla Brook" in r.json()["text"]
    assert "north" in r.json()["text"]


def test_nothing_to_report_is_204_and_no_body(client, store) -> None:
    """THE RULE. Noor has twelve notes; a rep with none has nothing to say, and
    an empty email is worse than no email."""
    store._rows["tenant-a"] = []
    r = client.get(f"{BRIEFS}/rep/501", headers=_headers())
    assert r.status_code == 204
    assert r.content == b""


def test_a_subject_the_directory_does_not_list_is_404(client) -> None:
    """Not 204: a CRM sending a wrong id would read a quiet day, for ever."""
    r = client.get(f"{BRIEFS}/rep/999", headers=_headers())
    assert r.status_code == 404
    assert r.json()["reason"] == "subject_not_found"


def test_an_unwired_store_is_503_and_never_an_empty_brief(client, monkeypatch) -> None:
    """503 rather than 204: an empty brief would tell a manager they had a
    quiet month when in fact nobody had wired the store up."""
    # Register item 155: nothing loaded at startup is what "unwired" means.
    monkeypatch.setattr(app.state, "judgement_store", None, raising=False)
    del app.dependency_overrides[get_judgement_store]
    r = client.get(f"{BRIEFS}/rep/501", headers=_headers())
    assert r.status_code == 503
    assert r.json()["reason"] == "brief_store_unavailable"


# --- what the route refuses -------------------------------------------------


def test_an_unknown_role_is_422(client) -> None:
    """The three roles are the three briefs that exist; a fourth would need a
    fourth brief, and there is not one."""
    r = client.get(f"{BRIEFS}/regional_manager/501", headers=_headers())
    assert r.status_code == 422


@pytest.mark.parametrize("subject_id", [0, -1])
def test_a_subject_id_below_one_is_422(client, subject_id: int) -> None:
    """ge=1 on every id, the same rule the request bodies hold."""
    r = client.get(f"{BRIEFS}/rep/{subject_id}", headers=_headers())
    assert r.status_code == 422


def test_the_rep_brief_reads_only_that_reps_rows(client) -> None:
    """The route narrows the store read by author, so a rep brief cannot widen
    into a colleague's work by forgetting a filter downstream."""
    r = client.get(f"{BRIEFS}/rep/502", headers=_headers())
    assert r.status_code == 200
    assert "over 12 notes" in r.json()["text"]
    assert "Idris Vale" not in r.json()["text"]


def test_a_brief_carries_no_ids_at_all(client) -> None:
    """The reason the directory is a backend ask."""
    r = client.get(f"{BRIEFS}/head_of_sales/401", headers=_headers())
    for user in PEOPLE:
        assert str(user.user_id) not in r.json()["text"]


def test_the_window_is_the_tenants_rolling_days(client, monkeypatch, store) -> None:
    """A row older than the window is outside it, and a rep with only old rows
    has nothing to report."""
    # Patched on the ROUTE module: it imported the name, so patching the
    # config module would leave the route holding the original reference. The
    # route resolves its rules with resolve_tenant_config since register item 97.
    short = dataclasses.replace(OPTED_IN, rolling_window_days=1)

    async def _short(_tenant: str):
        return short

    monkeypatch.setattr(judgement_routes, "resolve_tenant_config", _short)
    stale = JudgementRow.model_validate(
        _row(501, 1).model_dump(mode="json")
        | {"note_created_at": (datetime.now(UTC) - timedelta(days=5)).isoformat()}
    )
    store._rows["tenant-a"] = [stale] * 12
    r = client.get(f"{BRIEFS}/rep/501", headers=_headers())
    assert r.status_code == 204


# --- the JSON, the switch and the deadline (register item 154) --------------


def test_the_brief_is_text_period_lines_and_flagged_ids(client, store) -> None:
    """Four keys; the period in local dates; each line its three measures."""
    store._rows["tenant-a"][0] = _row(501, 100, flagged=True)
    body = client.get(f"{BRIEFS}/rep/501", headers=_headers()).json()
    # Register item 145 added the three id lists beside C5's four keys.
    assert set(body) == {
        "text",
        "period",
        "lines",
        "flagged_note_ids",
        "signals",
        "flagged_yesterday",
        "coaching",
    }
    assert set(body["period"]) == {"first_day", "last_day", "days"}
    assert body["period"]["days"] == 30
    line = body["lines"][0]
    assert (line["name"], line["period"]) == ("Idris Vale", "rolling")
    assert line["average_band"]["band"] == "good"
    assert line["flagged_share"]["counted"] == 1
    assert body["flagged_note_ids"] == [100]


def test_a_tenant_that_has_not_opted_in_is_403(client, monkeypatch) -> None:
    """The default is off: no person's figures until the tenant says so."""

    async def _default(tenant: str):
        return get_tenant_config(tenant)

    monkeypatch.setattr(judgement_routes, "resolve_tenant_config", _default)
    r = client.get(f"{BRIEFS}/rep/501", headers=_headers())
    assert r.status_code == 403
    assert r.json()["reason"] == "rep_numbers_not_enabled"


def test_a_slow_store_is_503_at_the_deadline(client, monkeypatch, store) -> None:
    """The brief runs under the judgement deadline with its own code."""
    monkeypatch.setenv("DODEAL_JUDGEMENT_DEADLINE_SECONDS", "0.05")
    get_settings.cache_clear()

    async def _slow(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(store, "rows_between", _slow)
    r = client.get(f"{BRIEFS}/rep/501", headers=_headers())
    assert r.status_code == 503
    assert r.json()["reason"] == "brief_deadline_exceeded"


def test_the_switch_is_settable_through_the_section_parser() -> None:
    """One parser for the file and the admin route: the switch is in it."""
    from dodeal_ai.units.structured_intelligence.config import parse_unit_a_section

    config = parse_unit_a_section({"config_version": "v", "rep_numbers_enabled": True})
    assert config.rep_numbers_enabled is True
    assert get_tenant_config("tenant-a").rep_numbers_enabled is False
