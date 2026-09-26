"""A paid pass (Unit B, waves 1 and 2): kept, counted, and started at most twice.

NOTHING RECEIVED IS PAID FOR TWICE. Each pass's outcome -- its validated
answer, or why it failed -- is kept in the job's work (core/jobs.py) the
moment it is known, and a later run reads it instead of calling. Each start of
a pass is counted in db3 before the paid call:

  - a response that never arrived (model_unavailable, or a run cut off
    mid-call) may be started ONCE more, in this run or the next -- the lead's
    override of the never-retry rule for this case (BRD B6); after two
    starts the pass has failed;
  - a malformed answer has already had its one reprompt (llm_call.py) and
    fails the pass at once: a response we received is never paid for again.

The tokens each pass spent in a run are counted per pass for its outcome line,
both answers of a reprompt included, and apart from them the reasoning tokens
the provider reported: a count only, never the reasoning itself.

EACH PASS IS STAMPED with the provider and the model that answered it (a
route may send passes to different providers): kept with its answer, so a
later run reports the same stamp without calling again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

from dodeal_ai.core.config import ModelProfile
from dodeal_ai.core.errors import MalformedOutputError, ModelUnavailableError
from dodeal_ai.core.jobs import Job, store_work
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import ProfileSource
from dodeal_ai.core.prompting import AssembledPrompt

# Starts a pass may make, each paid: the first and ONE more when the first
# response never arrived (BRD B6, the lead's override). Never a third.
PASS_TRIES = 2
PASS_INTERRUPTED = "pass_interrupted"

# Counts one more start of a pass before its paid call: its starts after this
# one, or None when the job has moved on and nothing is left to finish.
type Starter = Callable[..., Awaitable[int | None]]


class PassFailed(Exception):
    """A pass that has failed for good; str() is the reason code."""


@dataclass(frozen=True, slots=True)
class Stamp:
    """Who answered a pass: the provider's safe name (None for a fake or an
    answer kept before stamps existed) and the model it reported."""

    provider: str | None
    model: str

    def to_dict(self) -> dict[str, str | None]:
        return {"provider": self.provider, "model": self.model}


class JobGone(Exception):
    """The job moved on or expired while its passes ran: nothing to finish."""


@dataclass(slots=True)
class PassUsage:
    """Tokens per pass, as the provider reported them, in this run; the part
    of each pass's output that was reasoning; and what each field-level
    pass's failed quotes dropped or left unverified."""

    tokens: dict[str, dict[str, int]] = field(default_factory=dict)
    reasoning: dict[str, int] = field(default_factory=dict)
    # Per field-level pass (the extraction, the extras): the fields its failed
    # quotes dropped, and those kept unverified; counts only, never a word.
    dropped: dict[str, int] = field(default_factory=dict)
    unverified: dict[str, int] = field(default_factory=dict)

    def evidence(self, name: str, *, dropped: int, unverified: int) -> None:
        """A field-level pass's count of fields its failed quotes cost."""
        self.dropped[name] = dropped
        self.unverified[name] = unverified

    def add(self, name: str, response: LLMResponse) -> None:
        spent = self.tokens.setdefault(name, {"input": 0, "output": 0, "calls": 0})
        spent["input"] += response.input_tokens
        spent["output"] += response.output_tokens
        spent["calls"] += 1
        self.reasoning[name] = self.reasoning.get(name, 0) + response.reasoning_tokens


class _Metered:
    """An LLMClient that counts every response of one pass into the usage, and
    hands the adapter the pass's schema for a json_schema profile."""

    def __init__(
        self,
        client: LLMClient,
        usage: PassUsage,
        name: str,
        schema: Mapping[str, object],
    ) -> None:
        self._client, self._usage, self._name = client, usage, name
        self._schema = schema

    def profile_for(self, name: str) -> ModelProfile | None:
        """The routed client's profile for `name`, so a ceiling follows it."""
        if isinstance(self._client, ProfileSource):
            return self._client.profile_for(name)
        return None

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        response = await self._client.complete(
            prompt,
            profile=profile,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema or self._schema,
        )
        self._usage.add(self._name, response)
        return response


@dataclass(frozen=True, slots=True)
class PassRun:
    """What a wave's passes need to be started, kept and counted."""

    job: Job
    work: dict[str, dict[str, object]]
    ttl_seconds: int
    client: LLMClient
    usage: PassUsage
    start: Starter


async def _keep(run: PassRun, name: str, outcome: dict[str, object]) -> None:
    await store_work(
        run.job.tenant, run.job.job_id, name, outcome, ttl_seconds=run.ttl_seconds
    )


async def run_pass[M: BaseModel](
    run: PassRun,
    name: str,
    schema: type[M],
    call: Callable[[LLMClient], Awaitable[tuple[M, LLMResponse]]],
) -> tuple[M, Stamp]:
    """The pass's answer and who answered it: kept from an earlier run, or
    paid for now under the retry-once rule. PassFailed when it has failed."""
    kept = run.work.get(name)
    if kept is not None:
        if "failed" in kept:
            raise PassFailed(f"{name}_{kept['failed']}")
        provider = kept.get("provider")
        return schema.model_validate(kept["answer"]), Stamp(
            None if provider is None else str(provider), str(kept["model"])
        )
    starts = run.job.passes.get(name, 0)
    while True:
        if starts >= PASS_TRIES:
            raise await _failed(run, name, PASS_INTERRUPTED)
        counted = await run.start(run.job, name, now=datetime.now(UTC))
        if counted is None:
            raise JobGone()
        starts = counted
        try:
            metered = _Metered(run.client, run.usage, name, schema.model_json_schema())
            answer, response = await call(metered)
        except ModelUnavailableError:
            if starts >= PASS_TRIES:
                raise await _failed(run, name, "model_unavailable")
            continue
        except MalformedOutputError:
            raise await _failed(run, name, "malformed_output")
        stamp = Stamp(response.provider, response.model)
        kept = {"answer": answer.model_dump(mode="json"), **stamp.to_dict()}
        await _keep(run, name, kept)
        return answer, stamp


async def _failed(run: PassRun, name: str, reason: str) -> PassFailed:
    """Keep the pass's failure, so no later run pays for it; the error to raise."""
    await _keep(run, name, {"failed": reason})
    return PassFailed(f"{name}_{reason}")
