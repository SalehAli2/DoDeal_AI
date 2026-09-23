"""Wave 2 (Unit B): the stage-2 passes on a done call's transcript, one after
another, each on its own profile.

  objections  unit_b.objections  the client's objections (objections.py)

EACH PASS STANDS ALONE. Its answer becomes its part; a pass that fails --
twice unanswered, or malformed after its one reprompt -- leaves its part null
with a reason (`objections_malformed_output`), and the passes after it still
run. Every pass is kept, counted and started at most twice (paid.py), so a
re-run of stage 2 pays for nothing it already received.

The transcript is read as wave 1 read it: the prompt copy, masked under the
tenant's country code, the summary language decided in code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.jobs import Job, start_stage2_pass
from dodeal_ai.core.llm import LLMClient
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.objections import (
    Objections,
    find_objections,
    objections_part,
)
from dodeal_ai.units.call_intelligence.paid import (
    PassFailed,
    PassRun,
    PassUsage,
    run_pass,
)
from dodeal_ai.units.call_intelligence.transcriber import Transcript

OBJECTIONS = "objections"


@dataclass(slots=True)
class Wave2:
    """Each pass's part, or None with its reason; and the model per pass."""

    parts: dict[str, dict[str, object] | None] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)


async def wave2(
    client: LLMClient,
    job: Job,
    config: CallsConfig,
    transcript: Transcript,
    *,
    work: dict[str, dict[str, object]],
    scope: TenantScope,
    settings: Settings,
    usage: PassUsage,
) -> Wave2:
    """Wave 2 for one transcript. JobGone when stage 2 stopped under it."""
    call = CallText.of(transcript, country_code=config.phone_country_code)
    run = PassRun(
        job, work, config.result_ttl_seconds, client, usage, start_stage2_pass
    )
    wave = Wave2()
    try:
        found, model = await run_pass(
            run,
            OBJECTIONS,
            Objections,
            lambda metered: find_objections(
                metered, call, scope=scope, settings=settings
            ),
        )
        wave.parts[OBJECTIONS] = objections_part(found)
        wave.models[OBJECTIONS] = model
    except PassFailed as failed:
        wave.parts[OBJECTIONS] = None
        wave.reasons[OBJECTIONS] = str(failed)
    return wave
