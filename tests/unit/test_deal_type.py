"""deal_type, the lead's business line (Q13, answered): the deal-specific checks
run, and a mark is out of 100, only when the tenant's deal switch is on AND the
note's lead block names a deal_type. Otherwise today's 80. The value reaches no
prompt, and the lead block still refuses any other field."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE, for_lead
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response, stable_for
from tests.helpers.score_answers import score_payload

DIRECT = "/api/v1/notes/judgements/direct"
CONFIG_URL = "/api/v1/admin/tenant-config"
DEFAULT = get_tenant_config("tenant-a")
DS_CHECKS = ("ds_figures", "ds_subject", "ds_timing")
# An invented business line, distinctive enough to find in a prompt if it leaked.
DEAL = "invented-line-zq"


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def client(monkeypatch, llm) -> Iterator[TestClient]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    app.dependency_overrides[get_llm_client] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }


def _switch(client: TestClient, on: bool) -> None:
    response = client.put(
        CONFIG_URL, json={"deal_specifics_applicable": on}, headers=_headers()
    )
    assert response.status_code == 200


def _body(lead: dict, note_id: int = 10) -> dict:
    return {
        "lead_id": 1656,
        "note_id": note_id,
        "author_id": 27,
        "note_text": "Called the client, discussed the 3BR at 2.1M, viewing Tuesday.",
        "lead": {"leadType": None, "project": None, **lead},
    }


def _judge(client: TestClient, llm: FakeLLM, lead: dict, *, deal: bool) -> dict:
    """One direct judgement. `deal`: the score pass answers the three ds_
    checks too, as it must when they are asked (and must not otherwise)."""
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
    extra = dict.fromkeys(DS_CHECKS, True) if deal else {}
    llm.script_for(SCORE_TEMPLATE, json_response(score_payload(**extra)))
    response = client.post(DIRECT, json=_body(lead), headers=_headers())
    assert response.status_code == 200
    return response.json()


def _score_prompt(llm: FakeLLM) -> str:
    (prompt,) = [p for p in llm.prompts if p.stable == stable_for(SCORE_TEMPLATE)]
    return prompt.variable


def _deal_component(body: dict) -> dict:
    (component,) = [
        c for c in body["score"]["components"] if c["name"] == "deal_specifics"
    ]
    return component


def test_deal_type_with_the_switch_on_runs_the_ds_checks_out_of_100(
    client, llm
) -> None:
    _switch(client, on=True)
    body = _judge(client, llm, {"deal_type": DEAL}, deal=True)

    assert body["score"]["denominator"] == 100
    # 55 of 80 plus all three ds_ checks true: 75 of 100.
    assert body["score"]["total"] == 75
    assert _deal_component(body) == {
        "name": "deal_specifics",
        "mark": 20,
        "weight": 20,
        "suppressed": False,
    }
    asked = _score_prompt(llm)
    assert all(check in asked for check in DS_CHECKS)
    # A gate only: the business line itself reaches no prompt.
    assert all(DEAL not in prompt.text for prompt in llm.prompts)
    # The switch is mark-affecting, so the PUT moved the config_version.
    assert body["versions"]["config_version"] != DEFAULT.config_version


def test_no_deal_type_keeps_80_with_the_switch_on(client, llm) -> None:
    _switch(client, on=True)
    body = _judge(client, llm, {}, deal=False)

    assert body["score"]["denominator"] == 80
    assert _deal_component(body)["suppressed"] is True
    assert not any(check in _score_prompt(llm) for check in DS_CHECKS)


def test_a_blank_deal_type_is_absent(client, llm) -> None:
    _switch(client, on=True)
    body = _judge(client, llm, {"deal_type": "   "}, deal=False)

    assert body["score"]["denominator"] == 80


def test_the_switch_off_keeps_80_whatever_the_deal_type(client, llm) -> None:
    body = _judge(client, llm, {"deal_type": DEAL}, deal=False)

    assert body["score"]["denominator"] == 80
    assert body["versions"]["config_version"] == "tenant-cfg-default-5"
    assert not any(check in _score_prompt(llm) for check in DS_CHECKS)


@pytest.mark.parametrize(
    "lead",
    [
        {"deal_type": DEAL, "deal_kind": "sales"},
        {"business_line": "sales"},
        {"deal_type": "x" * 101},
    ],
)
def test_an_unknown_field_or_an_overlong_deal_type_is_422(client, llm, lead) -> None:
    response = client.post(DIRECT, json=_body(lead), headers=_headers())

    assert response.status_code == 422
    assert DEAL not in response.text
    assert llm.call_count == 0


def test_for_lead_turns_only_the_switch_off() -> None:
    """The one field it may move; config_version stays the tenant's."""
    on = dataclasses.replace(DEFAULT, deal_specifics_applicable=True)
    assert for_lead(on, DEAL) is on
    assert for_lead(DEFAULT, None) is DEFAULT
    off = for_lead(on, None)
    assert off.deal_specifics_applicable is False
    assert off.config_version == on.config_version
