"""What one task spent -- a judgement, or one run of a call job -- counted as it
happens and priced once, at its outcome line (register item "cost").

COUNTED WHERE IT IS PAID. Every model call already passes through
`complete_once` once, the reprompt included; `record_model_call` there adds it
to the task's Spend, so `model_calls` counts CALLS and a reprompted pass costs
two. Speech-to-text seconds are added by the call worker as it transcribes.

THE TASK'S SPEND RIDES A CONTEXT VARIABLE, set by `spending(unit)` around the
task. Gathered passes inherit the same object, so all three of a judgement's
passes land in one Spend without any signature learning about it.

PRICED FROM SETTINGS: MODEL_PRICES is USD per million tokens (input,
cached_input, output), STT_PRICES USD per audio minute, both keyed by the model
name the provider REPORTED. A model with no price makes the whole task's
cost null -- never a partial sum that reads as the real one -- and earns ONE
WARNING per task, naming the model and nothing else. Every line carries
PRICE_TABLE_VERSION, so a cost is read against the table that produced it.

Token kinds follow the OpenAI usage shape: cached input is PART OF input,
reasoning is PART OF output. Input is priced as (input - cached) at the input
rate plus cached at the cached rate.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from dodeal_ai.core import metrics
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import LLMResponse

_logger = logging.getLogger("dodeal_ai.cost")

_PER_MILLION = 1_000_000
_SECONDS_PER_MINUTE = 60

# The task being counted, or None outside one.
_CURRENT: ContextVar[Spend | None] = ContextVar("dodeal_spend", default=None)


@dataclass(slots=True)
class _Tokens:
    input: int = 0
    cached_input: int = 0
    output: int = 0
    reasoning: int = 0


@dataclass(slots=True)
class Spend:
    """One task's model calls, tokens and audio seconds, by model."""

    unit: str
    model_calls: int = 0
    tokens: dict[str, _Tokens] = field(default_factory=dict)
    audio: dict[str, int] = field(default_factory=dict)
    _cost: tuple[float | None] | None = None

    def record_call(self, response: LLMResponse) -> None:
        counts = self.tokens.setdefault(response.model, _Tokens())
        counts.input += response.input_tokens
        counts.cached_input += response.cached_input_tokens
        counts.output += response.output_tokens
        counts.reasoning += response.reasoning_tokens
        self.model_calls += 1
        self._cost = None

    def record_audio(self, model: str, seconds: int) -> None:
        self.audio[model] = self.audio.get(model, 0) + seconds
        self._cost = None

    def _total(self, kind: str) -> int:
        return sum(getattr(counts, kind) for counts in self.tokens.values())

    def cost_usd(self) -> float | None:
        """The task's cost in USD, or None when any model has no price."""
        if self._cost is None:
            self._cost = (self._priced(),)
        return self._cost[0]

    def _priced(self) -> float | None:
        settings = get_settings()
        total = 0.0
        for model, counts in self.tokens.items():
            price = settings.model_prices.get(model)
            if price is None:
                self._unpriced(model)
                return None
            total += (
                (counts.input - counts.cached_input) * price.input
                + counts.cached_input * price.cached_input
                + counts.output * price.output
            ) / _PER_MILLION
        for model, seconds in self.audio.items():
            per_minute = settings.stt_prices.get(model)
            if per_minute is None:
                self._unpriced(model)
                return None
            total += seconds / _SECONDS_PER_MINUTE * per_minute
        return round(total, 6)

    def _unpriced(self, model: str) -> None:
        _logger.warning(
            "price_unknown",
            extra={
                "reason_code": "price_unknown",
                "unit": self.unit,
                "model": model,
                "price_table_version": get_settings().price_table_version,
            },
        )

    def fields(self) -> dict[str, object]:
        """The spend fields every outcome line carries."""
        fields: dict[str, object] = {
            "model_calls": self.model_calls,
            "input_tokens": self._total("input"),
            "cached_input_tokens": self._total("cached_input"),
            "output_tokens": self._total("output"),
            "reasoning_tokens": self._total("reasoning"),
            "cost_usd": self.cost_usd(),
            "price_table_version": get_settings().price_table_version,
        }
        if self.unit == "unit_b":
            fields["audio_seconds"] = sum(self.audio.values())
        return fields

    def count(self, outcome: str) -> None:
        """task_cost_usd_total{unit, outcome}, when the cost is known."""
        cost = self.cost_usd()
        if cost is not None:
            metrics.TASK_COST_USD.labels(unit=self.unit, outcome=outcome).inc(cost)


@contextmanager
def spending(unit: str) -> Iterator[Spend]:
    """Count everything spent inside as one task of `unit`."""
    spend = Spend(unit=unit)
    token = _CURRENT.set(spend)
    try:
        yield spend
    finally:
        _CURRENT.reset(token)


def current_spend() -> Spend | None:
    """The task being counted, or None outside one."""
    return _CURRENT.get()


def record_model_call(response: LLMResponse, label: str) -> None:
    """One paid model call: into the task's Spend, and llm_tokens_total by
    unit, pass and kind. The label is `llm.<unit>.<pass>`."""
    spend = _CURRENT.get()
    if spend is not None:
        spend.record_call(response)
    parts = label.split(".")
    unit = parts[1] if len(parts) == 3 else "unknown"
    step = metrics.pass_name(label)
    for kind, count in (
        ("input", response.input_tokens),
        ("cached_input", response.cached_input_tokens),
        ("output", response.output_tokens),
        ("reasoning", response.reasoning_tokens),
    ):
        if count:
            metrics.LLM_TOKENS.labels(unit=unit, **{"pass": step}, kind=kind).inc(count)
            if unit == "unit_b":
                metrics.CALL_TOKENS.labels(**{"pass": step}, kind=kind).inc(count)
