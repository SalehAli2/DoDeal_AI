"""NoteAnalysis.checks (register item 148): the validated check answers on a
scored judgement, null when suppressed, never on a log line, and absent from
judgements stored before it existed, which still replay."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.conftest import RedisFakes
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import FAIR_CHECKS, score_payload

DIRECT = "/api/v1/notes/judgements/direct"
NOTE = "Called the client about the 3BR, discussed the price, following up."


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


def _script(llm: FakeLLM, note_type: str = "discovery") -> None:
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": note_type}))
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


def _post(client: TestClient, text: str = NOTE):
    body = {
        "lead_id": 1656,
        "note_id": 10,
        "author_id": 27,
        "note_text": text,
        "lead": {
            "leadType": None,
            "enquiryType": None,
            "project": None,
            "status": None,
        },
    }
    headers = {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }
    return client.post(DIRECT, json=body, headers=headers)


def test_a_scored_judgement_carries_its_validated_checks(client, llm) -> None:
    _script(llm)
    assert _post(client).json()["analysis"]["checks"] == FAIR_CHECKS


def test_a_suppressed_judgement_carries_none(client, llm) -> None:
    """A classifier suppression was never scored: null, not an empty map."""
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": "system_event"}))
    assert _post(client).json()["analysis"]["checks"] is None


def test_a_judgement_stored_before_the_field_still_replays(
    client, llm, redis_fakes: RedisFakes
) -> None:
    """The stored JSON has no `checks`; the replay validates with it null."""
    _script(llm)
    first = _post(client).json()
    stored = dict(first)
    stored["analysis"] = {k: v for k, v in first["analysis"].items() if k != "checks"}
    key = state._idempotency_key("tenant-a", 10, state.note_fingerprint(NOTE))
    redis_fakes.operational.store[key] = json.dumps(stored)

    again = _post(client)

    assert again.headers["Idempotent-Replay"] == "true"
    assert again.json()["analysis"]["checks"] is None
    assert llm.call_count == 3


def test_no_check_answer_reaches_a_log_line(client, llm) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        _script(llm)
        _post(client)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines
    for line in lines:
        assert "checks" not in line
        assert "wh_outcome" not in json.dumps(line)
