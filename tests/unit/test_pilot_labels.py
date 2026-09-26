"""Pilot labels (D-97): a company whose unit_a or unit_b section says
`pilot: true` has "pilot": true at the top level of every note judgement,
brief, per-agent measure and call status response; another company's carry no
such key at all, not even false. The call stage events are in test_callbacks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    FileJudgementStore,
    JudgementRow,
    get_judgement_store,
)
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.user_directory import (
    FileUserDirectory,
    Role,
    User,
    get_user_directory,
)
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import score_payload

PILOT, OTHER = "tenant-a", "tenant-b"
UNIT_A_URL = "/api/v1/admin/tenant-config"
UNIT_B_URL = "/api/v1/admin/tenant-config/unit_b"
AUDIO_HOST = "audio.tenant.example"
REP = User(user_id=501, name="Idris Vale", role=Role.REP, team="north")


def _row(note_id: int) -> JudgementRow:
    return JudgementRow.model_validate(
        {
            "note_id": note_id,
            "lead_id": 9000 + note_id,
            "author_id": REP.user_id,
            "note_created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "note_type": "discovery",
            "band": "good",
            "total": 72,
            "denominator": 80,
            "suppressed_reason": None,
            "prompt_sent": False,
            "enforcement_verdict": "allow",
            "rubric_version": "note_rubric_v2",
            "prompt_version": "unit_a_prompts_v3",
            "model_version": "invented-model-1",
            "config_version": "tenant-cfg-default-5",
        }
    )


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def client(monkeypatch, llm) -> AsyncIterator[httpx.AsyncClient]:
    """Both companies see figures; only PILOT is a pilot, in both sections."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    rows = [_row(100 + index) for index in range(12)]
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_judgement_store] = lambda: FileJudgementStore(
        {PILOT: rows, OTHER: rows}
    )
    app.dependency_overrides[get_user_directory] = lambda: FileUserDirectory(
        {PILOT: [REP], OTHER: [REP]}
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for tenant, pilot in ((PILOT, True), (OTHER, False)):
            unit_a = {"rep_numbers_enabled": True, **({"pilot": True} if pilot else {})}
            unit_b = {
                "calls_enabled": True,
                "audio_hosts": [AUDIO_HOST],
                **({"pilot": True} if pilot else {}),
            }
            for url, body in ((UNIT_A_URL, unit_a), (UNIT_B_URL, unit_b)):
                put = await c.put(url, json=body, headers=_headers(tenant))
                assert put.status_code == 200
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _headers(tenant: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token(subdomain=tenant)}",
        "Host": f"{tenant}.dodealcrm.com",
    }


def _labelled(body: dict, tenant: str) -> bool:
    """The rule under test: true at the top for the pilot, absent otherwise."""
    if tenant == PILOT:
        return body.get("pilot") is True
    return "pilot" not in body


@pytest.mark.parametrize("tenant", [PILOT, OTHER])
async def test_a_note_judgement(client, llm, tenant) -> None:
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": "discovery"}))
    llm.script_for(
        template_for(NoteType.DISCOVERY),
        json_response(
            {
                "is_vague": False,
                "missing_components": [],
                "clarification_prompt": None,
                "reasoning": "Complete.",
            }
        ),
    )
    llm.script_for(SCORE_TEMPLATE, json_response(score_payload()))
    body = {
        "lead_id": 1656,
        "note_id": 10,
        "author_id": 27,
        "note_text": "Called the client, discussed the 3BR, following up Tuesday.",
        "lead": {},
    }
    url = "/api/v1/notes/judgements/direct"
    first = await client.post(url, json=body, headers=_headers(tenant))
    replay = await client.post(url, json=body, headers=_headers(tenant))

    assert first.status_code == 200
    assert _labelled(first.json(), tenant)
    # The same judgement from the idempotency store is labelled the same way.
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert _labelled(replay.json(), tenant)


@pytest.mark.parametrize("tenant", [PILOT, OTHER])
async def test_a_brief(client, tenant) -> None:
    response = await client.get(
        f"/api/v1/briefs/rep/{REP.user_id}", headers=_headers(tenant)
    )
    assert response.status_code == 200
    assert _labelled(response.json(), tenant)


@pytest.mark.parametrize("tenant", [PILOT, OTHER])
async def test_a_per_agent_measure(client, tenant) -> None:
    rep = await client.get(
        f"/api/v1/measures/reps/{REP.user_id}", headers=_headers(tenant)
    )
    team = await client.get("/api/v1/measures/teams/north", headers=_headers(tenant))

    assert rep.status_code == team.status_code == 200
    assert _labelled(rep.json(), tenant)
    assert _labelled(team.json(), tenant)
    assert all(_labelled(member, tenant) for member in team.json()["members"])


@pytest.mark.parametrize("tenant", [PILOT, OTHER])
async def test_a_call_status_response(client, tenant) -> None:
    pushed = await client.post(
        "/api/v1/calls/jobs",
        json={
            "call_id": 7,
            "lead_id": 1656,
            "author_id": 27,
            "duration_seconds": 95,
            "recorded_at": "2026-09-23T08:00:00+04:00",
            "audio_url": f"https://{AUDIO_HOST}/7.wav",
            "audio_url_expires_at": "2026-09-23T10:00:00+04:00",
        },
        headers=_headers(tenant),
    )
    assert pushed.status_code == 202
    job_id = pushed.json()["job_id"]

    response = await client.get(
        f"/api/v1/calls/jobs/{job_id}", headers=_headers(tenant)
    )

    assert response.status_code == 200
    assert response.json()["job_id"] == job_id
    assert _labelled(response.json(), tenant)
