"""The daily cap on the nothing-to-ask path (register item 66): a rep already
past today's cap reads `rate_limited`, not `nothing_to_ask`, and the read takes
nothing and fails open."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.state import (
    _rate_limit_day_key,
    read_daily_rate_limit,
)
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.conftest import RedisFakes
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import score_payload

DAY_KEY = _rate_limit_day_key("tenant-a", "author:27")
DAILY_CAP = get_tenant_config("tenant-a").rate_limit_per_day


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def client(monkeypatch, llm):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    app.dependency_overrides[get_llm_client] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _flag_with_nothing_to_ask(llm: FakeLLM) -> dict:
    """A fair note (flagged) the vague pass found nothing to ask about."""
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
    return {
        "lead_id": 1656,
        "note_id": 10,
        "author_id": 27,
        "note_text": "Called the client about the 3BR, discussed the price.",
        "lead": {
            "leadType": None,
            "enquiryType": None,
            "project": None,
            "status": None,
        },
    }


def _post(client: TestClient, body: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }
    response = client.post(
        "/api/v1/notes/judgements/direct", json=body, headers=headers
    )
    return response.json()["decision"]


def test_past_the_daily_cap_nothing_to_ask_reads_rate_limited(
    client, llm, redis_fakes: RedisFakes
) -> None:
    redis_fakes.operational.store[DAY_KEY] = str(DAILY_CAP)
    decision = _post(client, _flag_with_nothing_to_ask(llm))
    assert decision["action"] == "accept_flag_prompt"
    assert decision["prompt_withheld"] == "rate_limited"
    assert redis_fakes.operational.store[DAY_KEY] == str(DAILY_CAP)


def test_under_the_daily_cap_it_is_nothing_to_ask(
    client, llm, redis_fakes: RedisFakes
) -> None:
    redis_fakes.operational.store[DAY_KEY] = str(DAILY_CAP - 1)
    decision = _post(client, _flag_with_nothing_to_ask(llm))
    assert decision["prompt_withheld"] == "nothing_to_ask"


async def test_the_daily_read_fails_open(redis_fakes: RedisFakes, caplog) -> None:
    redis_fakes.operational.raise_on.add("get")
    assert await read_daily_rate_limit("tenant-a", "author:27", request_id="r") == 0
    assert "rate_limit_bypassed" in caplog.text
