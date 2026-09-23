"""Load scenario 7 (register item 74): the primary provider answers 429 in a
burst, through a fake transport, while the fallback answers.

The real clients -- OpenAICompatibleClient behind FallbackLLMClient -- over
httpx.MockTransport, so the breaker, the fallback rule and the no-retry rule
are the production code's. Asserted: the primary's breaker opens; every paid
call reaches the fallback at most once; no prompt is sent twice to either
provider (nothing paid is retried); every judgement answers. The p95 latency is
printed and recorded as the user property `s7_p95_ms`.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import Counter

import httpx
import pytest
from pydantic import SecretStr

from dodeal_ai.core.breaker import BreakerState
from dodeal_ai.core.config import LLMProvider, get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.llm.fallback import FallbackLLMClient
from dodeal_ai.core.llm.openai_compatible import (
    GROQ_BASE_URL,
    OPENAI_BASE_URL,
    OpenAICompatibleClient,
)
from dodeal_ai.core.prompting import _read_template_file
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from tests.helpers import tokens
from tests.helpers.score_answers import score_payload

BURST = 15
MODEL = "invented-model-1"


@pytest.fixture(autouse=True)
def burst_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Room for the whole burst, and the service token the direct route needs."""
    monkeypatch.setenv("DODEAL_MAX_INFLIGHT", str(BURST))
    tokens.service_settings_env(monkeypatch)


def _content(prompt: str, classify: str, score: str) -> dict:
    """The answer a well-behaved model gives to the pass this prompt is."""
    if prompt.startswith(classify):
        return {"note_type": "discovery"}
    if prompt.startswith(score):
        return score_payload()
    return {
        "is_vague": False,
        "missing_components": [],
        "clarification_prompt": None,
        "reasoning": "Complete.",
    }


def _ok(content: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-s7",
            "model": MODEL,
            "choices": [
                {"message": {"content": json.dumps(content)}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


async def test_a_429_burst_opens_the_breaker_and_falls_back_once_per_call(
    lane, record_property
) -> None:
    classify = _read_template_file(CLASSIFY_TEMPLATE)
    score = _read_template_file(SCORE_TEMPLATE)
    primary_prompts: Counter[str] = Counter()
    fallback_prompts: Counter[str] = Counter()

    def _primary(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"]
        primary_prompts[prompt] += 1
        return httpx.Response(429)

    def _fallback(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"]
        fallback_prompts[prompt] += 1
        return _ok(_content(prompt, classify, score))

    settings = get_settings()
    primary = OpenAICompatibleClient(
        GROQ_BASE_URL,
        MODEL,
        SecretStr("change-me"),
        httpx.AsyncClient(transport=httpx.MockTransport(_primary)),
        settings=settings.model_copy(
            update={"llm_provider": LLMProvider.GROQ, "llm_model": MODEL}
        ),
    )
    fallback = OpenAICompatibleClient(
        OPENAI_BASE_URL,
        MODEL,
        SecretStr("change-me"),
        httpx.AsyncClient(transport=httpx.MockTransport(_fallback)),
        settings=settings.model_copy(
            update={"llm_provider": LLMProvider.OPENAI, "llm_model": MODEL}
        ),
    )
    client = FallbackLLMClient(primary, fallback)
    app.dependency_overrides[get_llm_client] = lambda: client

    # Distinct texts only: the corpus repeats one note, and a repeated text is a
    # repeated prompt that would read as a retry.
    distinct = {note.note: (lead_id, note) for lead_id, note in lane.notes}
    notes = list(distinct.values())[:BURST]
    assert len(notes) == BURST

    async def _one(lead_id, note) -> tuple[int, float]:
        started = time.monotonic()
        response = await lane.judge_direct(lead_id, note, note.note)
        return response.status_code, (time.monotonic() - started) * 1000

    results = await asyncio.gather(*(_one(lead_id, note) for lead_id, note in notes))

    statuses = [status for status, _ in results]
    assert statuses == [200] * BURST
    assert primary.breaker.state is BreakerState.OPEN
    # Three passes a judgement, each answered by the fallback exactly once.
    assert sum(fallback_prompts.values()) == 3 * BURST
    assert set(fallback_prompts.values()) == {1}
    # No paid call is ever sent twice to the primary, and once the breaker is
    # open the primary is not asked at all.
    assert set(primary_prompts.values()) <= {1}
    assert sum(primary_prompts.values()) < 3 * BURST
    p95 = _p95([latency for _, latency in results])
    record_property("s7_p95_ms", round(p95))
    print(f"S7 p95 {p95:.0f} ms over {BURST} judgements")
