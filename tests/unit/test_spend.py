"""Spend on both units' outcome lines (register item "cost"): every paid call
counted -- the reprompt included -- cached and reasoning tokens carried, a
cost priced from the tables or null with one warning, and a failed task still
saying what it spent."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core import metrics
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.cost.spend import (
    Spend,
    current_spend,
    record_model_call,
    spending,
)
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_llm import FAKE_MODEL, FakeLLM, json_response, response
from tests.helpers.score_answers import score_payload

DIRECT = "/api/v1/notes/judgements/direct"
PRICES = {FAKE_MODEL: {"input": 1.0, "cached_input": 0.5, "output": 2.0}}


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def client(monkeypatch, llm: FakeLLM) -> Iterator[TestClient]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    app.dependency_overrides[get_llm_client] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def json_log() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield stream
    logger.removeHandler(handler)
    logger.setLevel(original)


def _lines(stream: io.StringIO, message: str) -> list[dict]:
    parsed = [json.loads(line) for line in stream.getvalue().splitlines()]
    return [line for line in parsed if line["message"] == message]


def _priced(monkeypatch, version: str = "prices-2026-09") -> None:
    monkeypatch.setenv("DODEAL_MODEL_PRICES", json.dumps(PRICES))
    monkeypatch.setenv("DODEAL_PRICE_TABLE_VERSION", version)
    get_settings.cache_clear()


def _script(llm: FakeLLM, *, classify: tuple = (), **kwargs: int) -> None:
    llm.script_for(
        CLASSIFY_TEMPLATE,
        *classify,
        json_response({"note_type": "discovery"}, **kwargs),
    )
    llm.script_for(
        template_for(NoteType.DISCOVERY),
        json_response(
            {
                "is_vague": False,
                "missing_components": [],
                "clarification_prompt": None,
                "reasoning": "Complete.",
            },
            **kwargs,
        ),
    )
    llm.script_for(SCORE_TEMPLATE, json_response(score_payload(), **kwargs))


def _judge(client: TestClient) -> int:
    body = {
        "lead_id": 1656,
        "note_id": 81,
        "author_id": 27,
        "note_text": "Called the client, discussed the 3BR, following up Tuesday.",
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
    return client.post(DIRECT, json=body, headers=headers).status_code


# --- the guard ---------------------------------------------------------------------


def test_a_reprompt_is_counted_as_a_model_call(client, llm, json_log, monkeypatch):
    """Three passes, four paid calls: the malformed first classify counts."""
    _priced(monkeypatch)
    _script(llm, classify=(response("not json"),))
    assert _judge(client) == 200

    (line,) = _lines(json_log, "judgement_completed")
    assert (line["model_passes"], line["model_calls"]) == (3, 4)
    assert (line["input_tokens"], line["output_tokens"]) == (400, 80)
    assert line["cost_usd"] == pytest.approx((400 * 1.0 + 80 * 2.0) / 1e6)
    assert line["price_table_version"] == "prices-2026-09"


def test_an_unknown_model_is_a_null_cost_and_one_warning(client, llm, json_log):
    _script(llm)
    assert _judge(client) == 200
    (line,) = _lines(json_log, "judgement_completed")
    assert (line["cost_usd"], line["price_table_version"]) == (None, "unset")
    (warning,) = _lines(json_log, "price_unknown")
    assert (warning["level"], warning["model"], warning["unit"]) == (
        "WARNING",
        FAKE_MODEL,
        "unit_a",
    )


def test_cached_and_reasoning_tokens_are_carried_and_priced(
    client, llm, json_log, monkeypatch
) -> None:
    _priced(monkeypatch)
    _script(llm, cached_input_tokens=40, reasoning_tokens=5)
    assert _judge(client) == 200
    (line,) = _lines(json_log, "judgement_completed")
    assert (line["cached_input_tokens"], line["reasoning_tokens"]) == (120, 15)
    per_call = (60 * 1.0 + 40 * 0.5 + 20 * 2.0) / 1e6
    assert line["cost_usd"] == pytest.approx(3 * per_call)
    charged = _lines(json_log, "tokens_charged")
    assert {(x["cached_input_tokens"], x["reasoning_tokens"]) for x in charged} == {
        (40, 5)
    }


def test_a_failed_judgement_still_logs_what_it_spent(
    client, llm, json_log, monkeypatch
) -> None:
    _priced(monkeypatch)
    llm.script_for(CLASSIFY_TEMPLATE, response("not json"), response("still not"))
    before = (
        metrics.REGISTRY.get_sample_value(
            "task_cost_usd_total", {"unit": "unit_a", "outcome": "malformed_output"}
        )
        or 0.0
    )
    assert _judge(client) == 503

    (line,) = _lines(json_log, "judgement_spend")
    assert (line["reason_code"], line["model_calls"], line["route"]) == (
        "malformed_output",
        2,
        "direct",
    )
    assert line["cost_usd"] == pytest.approx(2 * 140 / 1e6)
    after = metrics.REGISTRY.get_sample_value(
        "task_cost_usd_total", {"unit": "unit_a", "outcome": "malformed_output"}
    )
    assert after == pytest.approx(before + 2 * 140 / 1e6)


def test_a_suppressed_judgement_carries_its_spend(client, llm, json_log) -> None:
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": "system_event"}))
    assert _judge(client) == 200
    (line,) = _lines(json_log, "judgement_suppressed")
    assert (line["model_calls"], line["input_tokens"]) == (1, 100)


def test_the_token_metric_counts_by_unit_pass_and_kind(client, llm) -> None:
    def sample() -> float:
        labels = {"unit": "unit_a", "pass": "classify", "kind": "input"}
        return metrics.REGISTRY.get_sample_value("llm_tokens_total", labels) or 0.0

    before = sample()
    _script(llm, classify=(response("not json"),))
    _judge(client)
    assert sample() == before + 200


# --- the accumulator on its own ---------------------------------------------------


def test_a_call_outside_any_task_is_metered_but_not_accumulated() -> None:
    assert current_spend() is None
    record_model_call(response("{}"), "not-a-three-part-label")
    labels = {"unit": "unknown", "pass": "not-a-three-part-label", "kind": "input"}
    assert metrics.REGISTRY.get_sample_value("llm_tokens_total", labels)


def test_unit_b_spend_prices_audio_and_counts_call_tokens(monkeypatch) -> None:
    monkeypatch.setenv("DODEAL_STT_PRICES", json.dumps({"fake-stt-1": 0.006}))
    get_settings.cache_clear()
    labels = {"pass": "summary", "kind": "output"}
    before = metrics.REGISTRY.get_sample_value("call_tokens_total", labels) or 0.0
    with spending("unit_b") as spend:
        spend.record_audio("fake-stt-1", 150)
        spend.record_audio("fake-stt-1", 30)
        record_model_call(response("{}", model="m"), "llm.unit_b.summary")
        assert current_spend() is spend
    assert current_spend() is None
    assert metrics.REGISTRY.get_sample_value("call_tokens_total", labels) == before + 20
    fields = spend.fields()
    assert (fields["audio_seconds"], fields["model_calls"]) == (180, 1)
    assert fields["cost_usd"] is None
    only_audio = Spend(unit="unit_b")
    only_audio.record_audio("fake-stt-1", 150)
    assert only_audio.cost_usd() == pytest.approx(150 / 60 * 0.006)
    assert "audio_seconds" not in Spend(unit="unit_a").fields()


def test_an_unpriced_task_counts_no_cost() -> None:
    before = metrics.REGISTRY.get_sample_value(
        "task_cost_usd_total", {"unit": "unit_b", "outcome": "done"}
    )
    spend = Spend(unit="unit_b")
    spend.record_audio("nobody-priced-this", 60)
    spend.count("done")
    after = metrics.REGISTRY.get_sample_value(
        "task_cost_usd_total", {"unit": "unit_b", "outcome": "done"}
    )
    assert after == before
