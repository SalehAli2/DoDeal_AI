"""Strict blocking at a stage change (register item 142): a block needs all five
conditions, refuses the stage change and never the note, turns into a flag on
a resubmission, and the stage itself goes nowhere but one boolean."""

from __future__ import annotations

import dataclasses
import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.api.routes import judgements as judgement_routes
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import (
    get_tenant_config,
    parse_unit_a_section,
)
from dodeal_ai.units.structured_intelligence.decide import enforcement
from dodeal_ai.units.structured_intelligence.schemas import (
    DecisionAction,
    EnforcementMode,
    EnforcementTarget,
    EnforcementVerdict,
    NoteType,
    SuppressedDetail,
)
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import FAIR_CHECKS, score_payload

STRICT = dataclasses.replace(
    get_tenant_config("tenant-a"),
    enforcement_mode=EnforcementMode.STRICT,
    blocking_enabled=True,
)

_ALL_TRUE = {
    "config": STRICT,
    "action": DecisionAction.PROMPT_CLARIFICATION,
    "stage_change": True,
    "resubmission": False,
}


def _verdict(**changes: object):
    arguments = {**_ALL_TRUE, **changes}
    config = arguments.pop("config")
    return enforcement(config, **arguments)  # type: ignore[arg-type]


# --- the five conditions ----------------------------------------------------


def test_all_five_conditions_block_the_stage_change() -> None:
    """Strict, enabled, a blocking stage, not a resubmission, a note we'd ask about."""
    block = _verdict()
    assert (block.verdict, block.applies_to, block.mode) == (
        EnforcementVerdict.BLOCK,
        EnforcementTarget.STAGE_CHANGE,
        EnforcementMode.STRICT,
    )


@pytest.mark.parametrize(
    ("condition", "change"),
    [
        (
            "mode advisory",
            {
                "config": dataclasses.replace(
                    STRICT, enforcement_mode=EnforcementMode.ADVISORY
                )
            },
        ),
        (
            "blocking off",
            {"config": dataclasses.replace(STRICT, blocking_enabled=False)},
        ),
        ("no blocking stage", {"stage_change": False}),
        ("a resubmission", {"resubmission": True}),
        ("a note we would only flag", {"action": DecisionAction.ACCEPT_FLAG_PROMPT}),
    ],
)
def test_turning_any_one_condition_off_gives_no_block(condition, change) -> None:
    """Each condition alone withholds the block; the note is flagged instead."""
    result = _verdict(**change)
    assert result.verdict is EnforcementVerdict.FLAG, condition
    assert result.applies_to is EnforcementTarget.NOTE


def test_an_unrecognised_thin_note_blocks_and_a_long_one_does_not() -> None:
    """The one suppression that is the rep's to fix blocks; our own limits do not."""
    thin = enforcement(
        STRICT, detail=SuppressedDetail.NOTE_TOO_SHORT, stage_change=True
    )
    long = enforcement(STRICT, detail=SuppressedDetail.NOTE_TOO_LONG, stage_change=True)
    assert thin.verdict is EnforcementVerdict.BLOCK
    assert long.verdict is EnforcementVerdict.ALLOW


def test_an_accepted_note_is_allowed_at_a_blocking_stage() -> None:
    assert (
        _verdict(action=DecisionAction.ACCEPT_SILENT).verdict
        is EnforcementVerdict.ALLOW
    )


def test_the_switches_are_set_through_the_section_parser_casefolded() -> None:
    """One parser for the file and the admin route; stages stored casefolded."""
    config = parse_unit_a_section(
        {
            "config_version": "v",
            "enforcement_mode": "strict",
            "blocking_enabled": True,
            "blocking_stages": ["Qualified", "SIGNED"],
        }
    )
    assert config.blocking_enabled is True
    assert config.blocking_stages == frozenset({"qualified", "signed"})
    assert get_tenant_config("tenant-a").blocking_enabled is False


# --- through the routes ------------------------------------------------------

DIRECT = "/api/v1/notes/judgements/direct"
DIRECT_RESUBMIT = "/api/v1/notes/judgements/direct/resubmission"
HISTORY = "/api/v1/notes/judgements/history"
STAGE_SENTINEL = "Won-SENTINEL-stage"


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def client(monkeypatch, llm):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    strict = dataclasses.replace(
        STRICT, blocking_stages=frozenset({"won", STAGE_SENTINEL.casefold()})
    )

    async def _strict(_tenant: str):
        return strict

    monkeypatch.setattr(judgement_routes, "resolve_tenant_config", _strict)
    app.dependency_overrides[get_llm_client] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _vague(llm: FakeLLM) -> None:
    """A note we would ask about: vague, and scored below the flag threshold."""
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": "discovery"}))
    llm.script_for(
        template_for(NoteType.DISCOVERY),
        json_response(
            {
                "is_vague": True,
                "missing_components": ["next_step_with_date"],
                "clarification_prompt": "When is the follow-up?",
                "reasoning": "No date.",
            }
        ),
    )
    nothing = dict.fromkeys(FAIR_CHECKS, False)
    llm.script_for(SCORE_TEMPLATE, json_response(score_payload(nothing)))


def _body(text: str = "Called the client about the unit, will follow up.", **extra):
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
    body.update(extra)
    return body


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }


def test_a_casefolded_blocking_stage_blocks_on_the_direct_route(client, llm) -> None:
    _vague(llm)
    body = client.post(
        DIRECT, json=_body(stage_change_to="WON"), headers=_headers()
    ).json()
    assert body["decision"]["action"] == "prompt_clarification"
    assert body["enforcement"] == {
        "mode": "strict",
        "verdict": "block",
        "applies_to": "stage_change",
    }


def test_a_stage_outside_the_list_does_not_block(client, llm) -> None:
    _vague(llm)
    body = client.post(
        DIRECT, json=_body(stage_change_to="negotiation"), headers=_headers()
    ).json()
    assert body["enforcement"]["verdict"] == "flag"


def test_the_resubmission_route_flags_instead_of_blocking(client, llm) -> None:
    """A rep who answered the question is never locked out."""
    _vague(llm)
    body = client.post(
        DIRECT_RESUBMIT, json=_body(stage_change_to="won"), headers=_headers()
    ).json()
    assert body["enforcement"]["verdict"] == "flag"


def test_a_thin_note_at_a_blocking_stage_blocks(client) -> None:
    body = client.post(
        DIRECT, json=_body("ok", stage_change_to="won"), headers=_headers()
    ).json()
    assert body["suppressed"]["detail_code"] == "note_too_short"
    assert body["enforcement"]["verdict"] == "block"


def test_history_cannot_carry_the_field(client, llm) -> None:
    """422 on the history body, before anything is judged."""
    body = _body(stage_change_to="won", note_created_at="2026-03-04T09:15:00+04:00")
    r = client.post(HISTORY, json=body, headers=_headers())
    assert r.status_code == 422
    assert llm.call_count == 0


def test_a_stage_over_one_hundred_characters_is_422(client, llm) -> None:
    r = client.post(DIRECT, json=_body(stage_change_to="x" * 101), headers=_headers())
    assert r.status_code == 422


def test_the_stage_is_never_logged(client, llm) -> None:
    """The stage becomes one boolean; its text reaches no log line."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        _vague(llm)
        client.post(
            DIRECT, json=_body(stage_change_to=STAGE_SENTINEL), headers=_headers()
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert stream.getvalue()
    assert STAGE_SENTINEL not in stream.getvalue()
    assert STAGE_SENTINEL.casefold() not in stream.getvalue()
    for line in stream.getvalue().splitlines():
        assert "stage_change_to" not in json.loads(line)
