"""The script rule (register item 34): one module decides arabic, english or
mixed, every judgement carries it, and the fixed question and the eval runner
read the same answer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence import language as language_module
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.language import Language, language_of
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from scripts import run_eval
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import score_payload


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("اتصلت بالعميل وسأعاود الاتصال غداً", Language.ARABIC),
        ("Called the client, calling back tomorrow.", Language.ENGLISH),
        ("Called العميل tomorrow", Language.MIXED),
        ("0501234567 !!", Language.ENGLISH),
    ],
)
def test_the_three_buckets_by_script(text, expected) -> None:
    """Arabic alone, Latin alone (or neither), and both."""
    assert language_of(text) is expected


def test_the_eval_runner_reads_the_same_rule() -> None:
    """run_eval imports the one function; there is no second copy."""
    assert run_eval.language_of is language_module.language_of


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


def _post(client: TestClient, text: str) -> dict:
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
    return client.post(
        "/api/v1/notes/judgements/direct", json=body, headers=headers
    ).json()


def test_a_scored_mixed_note_carries_mixed(client, llm) -> None:
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
    body = _post(client, "Called the client about the villa, العميل مهتم جداً")
    assert body["analysis"]["language"] == "mixed"


def test_a_thin_arabic_note_is_arabic_and_asked_in_arabic(client) -> None:
    """The suppressed judgement carries its language; the fixed question
    follows the same rule."""
    body = _post(client, "لا")
    assert body["analysis"]["language"] == "arabic"
    assert language_of(body["suppressed"]["clarification_prompt"]) is Language.ARABIC


def test_a_thin_english_note_is_asked_in_english(client) -> None:
    body = _post(client, "ok")
    assert body["analysis"]["language"] == "english"
    assert language_of(body["suppressed"]["clarification_prompt"]) is Language.ENGLISH
