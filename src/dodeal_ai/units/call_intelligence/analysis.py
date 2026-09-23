"""Wave 1 (Unit B): what stage 1 says about a transcribed call, beside the
transcript -- the language and the two model passes (the analysis), and the
signals, numbers, alarm phrases and escalations found in code (the signals).

THE SIGNALS BLOCK IS ALWAYS THERE for a transcript: it is found in code before
any pass starts and goes out top-level in stage 1, whatever becomes of the
passes -- so a model outage still delivers an off_channel_contact escalation.

EVERY TRANSCRIPT GETS IT: a call from min_transcribe_seconds up (BRD B8), and
an uncertain one too, with every field marked uncertain (passes.settled).

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

A PASS THAT FAILS still lets stage 1 go: analysis is null and analysis_reason
says which pass and why (`extract_model_unavailable`, `prose_malformed_output`,
`extract_pass_interrupted`, or `llm_not_configured` for a run with no client).
The prose pass is not started once the extraction has failed.

The tokens each pass spent in this run are counted per pass for the outcome
line, both answers of a reprompt included.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.errors import MalformedOutputError, ModelUnavailableError
from dodeal_ai.core.jobs import Job, start_pass, store_work
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.prompting import AssembledPrompt
from dodeal_ai.units.call_intelligence.alarms import alarms_if_enabled
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.numbers import numbers_if_enabled
from dodeal_ai.units.call_intelligence.passes import (
    DETAIL_NAMES,
    CallText,
    Extraction,
    Prose,
    extract,
    mood_uncertain,
    settled,
    write_prose,
)
from dodeal_ai.units.call_intelligence.prompts import PROMPT_SET_VERSION
from dodeal_ai.units.call_intelligence.signals import SIGNALS_VERSION, call_signals
from dodeal_ai.units.call_intelligence.transcriber import Transcript

EXTRACT = "extract"
PROSE = "prose"

# Starts a pass may make, each paid: the first and ONE more when the first
# response never arrived (BRD B6, the lead's override). Never a third.
PASS_TRIES = 2
PASS_INTERRUPTED = "pass_interrupted"
NO_CLIENT = "llm_not_configured"


class PassFailed(Exception):
    """A pass that has failed for good; str() is the reason code."""


class JobGone(Exception):
    """The job went terminal or expired while wave 1 ran: nothing to finish."""


@dataclass(slots=True)
class PassUsage:
    """Tokens per pass, as the provider reported them, in this run."""

    tokens: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, name: str, response: LLMResponse) -> None:
        spent = self.tokens.setdefault(name, {"input": 0, "output": 0, "calls": 0})
        spent["input"] += response.input_tokens
        spent["output"] += response.output_tokens
        spent["calls"] += 1


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
class Wave1:
    """Stage 1's analysis, or null with the reason; the signals block found in
    code, never null; and the versions both ran on."""

    analysis: dict[str, object] | None
    reason: str | None
    versions: dict[str, object]
    signals: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Run:
    """What one pass needs to be started, kept and counted."""

    job: Job
    work: dict[str, dict[str, object]]
    ttl_seconds: int
    client: LLMClient
    usage: PassUsage


async def _keep(run: _Run, name: str, outcome: dict[str, object]) -> None:
    await store_work(
        run.job.tenant, run.job.job_id, name, outcome, ttl_seconds=run.ttl_seconds
    )


async def _pass[M: BaseModel](
    run: _Run,
    name: str,
    schema: type[M],
    call: Callable[[LLMClient], Awaitable[tuple[M, LLMResponse]]],
) -> tuple[M, str]:
    """The pass's answer and the model it came from: kept from an earlier run,
    or paid for now under the retry-once rule. PassFailed when it has failed."""
    kept = run.work.get(name)
    if kept is not None:
        if "failed" in kept:
            raise PassFailed(f"{name}_{kept['failed']}")
        return schema.model_validate(kept["answer"]), str(kept["model"])
    starts = run.job.passes.get(name, 0)
    while True:
        if starts >= PASS_TRIES:
            raise await _failed(run, name, PASS_INTERRUPTED)
        counted = await start_pass(run.job, name, now=datetime.now(UTC))
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
        kept = {"answer": answer.model_dump(mode="json"), "model": response.model}
        await _keep(run, name, kept)
        return answer, response.model


async def _failed(run: _Run, name: str, reason: str) -> PassFailed:
    """Keep the pass's failure, so no later run pays for it; the error to raise."""
    await _keep(run, name, {"failed": reason})
    return PassFailed(f"{name}_{reason}")


def _analysis(
    call: CallText, extraction: Extraction, prose: Prose
) -> dict[str, object]:
    """The analysis block: the passes' answers with code's word on certainty."""
    kept = settled(extraction, call)
    return {
        "language": call.language,
        "uncertain": call.uncertain,
        "summary": prose.summary,
        "elements": kept.model_dump(
            mode="json",
            include={
                "wanted",
                "discussed",
                "concerns",
                "agreed",
                "next_step",
                "ending",
            },
        ),
        "details": {
            name: getattr(kept.details, name).model_dump(mode="json")
            for name in DETAIL_NAMES
        },
        "mood": {
            **kept.mood.model_dump(mode="json"),
            "uncertain": mood_uncertain(extraction, call),
        },
        "crm_note": prose.crm_note,
    }


def _found_in_code(
    transcript: Transcript, config: CallsConfig, job: Job
) -> tuple[dict[str, object], str | None]:
    """The signals block -- talk signals, numbers, alarm phrases and their
    escalations, with no model -- and the alarm list's digest."""
    segments = transcript.segments
    lead, agent = (
        job.metadata.get("lead_phone_hash"),
        job.metadata.get("agent_phone_hash"),
    )
    numbers = numbers_if_enabled(
        segments,
        enabled=config.number_detection_enabled,
        lead_phone_hash=lead if isinstance(lead, str) else None,
        agent_phone_hash=agent if isinstance(agent, str) else None,
        country_code=config.phone_country_code,
    )
    alarms = alarms_if_enabled(
        segments, enabled=config.alarm_phrases_enabled, phrases=config.alarm_phrases
    )
    escalations = [
        *([] if numbers is None else numbers.escalations),
        *([] if alarms is None else alarms.escalations),
    ]
    escalations.sort(key=lambda escalation: float(str(escalation["start_s"])))
    found: dict[str, object] = {
        **call_signals(segments),
        "numbers": None if numbers is None else numbers.finds,
        "alarms": None if alarms is None else alarms.finds,
        "escalations": escalations,
    }
    return found, None if alarms is None else alarms.digest


async def wave1(
    client: LLMClient | None,
    job: Job,
    config: CallsConfig,
    transcript: Transcript,
    *,
    work: dict[str, dict[str, object]],
    scope: TenantScope,
    settings: Settings,
    usage: PassUsage,
) -> Wave1:
    """Wave 1 for one transcript. JobGone when the job stopped under it."""
    call = CallText.of(transcript, country_code=config.phone_country_code)
    code, digest = _found_in_code(transcript, config, job)
    models: list[str] = []
    versions: dict[str, object] = {
        "prompt": PROMPT_SET_VERSION,
        "signals": SIGNALS_VERSION,
        "model": None,
        "transcriber": f"{transcript.provider}/{transcript.model}",
        "alarm_list_digest": digest,
    }
    if client is None:
        return Wave1(None, NO_CLIENT, versions, code)
    run = _Run(job, work, config.result_ttl_seconds, client, usage)
    try:
        extraction, model = await _pass(
            run,
            EXTRACT,
            Extraction,
            lambda metered: extract(metered, call, scope=scope, settings=settings),
        )
        models.append(model)
        doubted = settled(extraction, call)
        prose, model = await _pass(
            run,
            PROSE,
            Prose,
            lambda metered: write_prose(
                metered, call, doubted, scope=scope, settings=settings
            ),
        )
        models.append(model)
    except PassFailed as failed:
        versions["model"] = ",".join(sorted(set(models))) or None
        return Wave1(None, str(failed), versions, code)
    versions["model"] = ",".join(sorted(set(models)))
    return Wave1(_analysis(call, extraction, prose), None, versions, code)
