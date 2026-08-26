"""FakeLLM satisfies the seam and behaves like a strict script."""

from __future__ import annotations

import pytest

from dodeal_ai.core.llm import FinishReason, LLMClient, LLMErrorReason, LLMProviderError
from dodeal_ai.core.prompting import AssembledPrompt
from tests.helpers.fake_llm import FakeLLM, FakeLLMExhausted, response, truncated

PROMPT = AssembledPrompt(stable="S", variable="V")


def test_satisfies_protocol_statically_and_at_runtime() -> None:
    client: LLMClient = FakeLLM()  # mypy checks the signature here
    assert isinstance(client, LLMClient)


async def test_pops_responses_in_order_and_counts_calls() -> None:
    fake = FakeLLM(response("one"), response("two"))
    assert (await fake.complete(PROMPT)).text == "one"
    assert (await fake.complete(PROMPT)).text == "two"
    assert fake.call_count == 2


async def test_records_prompt_object_unchanged_and_kwargs() -> None:
    fake = FakeLLM(response("x"))
    await fake.complete(PROMPT, max_output_tokens=256)
    assert fake.prompts[0] is PROMPT
    assert fake.calls[0].max_output_tokens == 256


async def test_default_max_output_tokens_is_none() -> None:
    fake = FakeLLM(response("x"))
    await fake.complete(PROMPT)
    assert fake.calls[0].max_output_tokens is None


async def test_scripted_exception_is_raised_and_still_recorded() -> None:
    fake = FakeLLM(
        LLMProviderError(LLMErrorReason.RATE_LIMITED, transient=True), response("ok")
    )
    with pytest.raises(LLMProviderError):
        await fake.complete(PROMPT)
    assert (await fake.complete(PROMPT)).text == "ok"
    assert fake.call_count == 2


async def test_exhausted_script_raises_not_defaults() -> None:
    fake = FakeLLM(response("only"))
    await fake.complete(PROMPT)
    with pytest.raises(FakeLLMExhausted):
        await fake.complete(PROMPT)
    assert fake.call_count == 2  # the failing call is still recorded


async def test_script_can_be_extended_mid_test() -> None:
    fake = FakeLLM()
    fake.script(response("late"))
    assert (await fake.complete(PROMPT)).text == "late"


def test_response_factory_carries_configurable_tokens() -> None:
    r = response("x", input_tokens=7, output_tokens=3)
    assert r.total_tokens == 10
    assert r.finish_reason is FinishReason.STOP


def test_truncated_sets_max_tokens() -> None:
    assert truncated("cut").finish_reason is FinishReason.MAX_TOKENS
