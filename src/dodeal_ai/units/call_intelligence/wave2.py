"""Wave 2 (Unit B): the stage-2 passes on a done call's transcript, one after
another, each on its own profile.

  objections  unit_b.objections  the client's objections (objections.py)
  score       unit_b.score       yes-or-no checks, marked in code (score.py);
                                 not run at all when the call gets no score

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
from dodeal_ai.units.call_intelligence.prompts import AGENT, CLIENT
from dodeal_ai.units.call_intelligence.score import (
    ScoreChecks,
    ask_checks,
    score_call,
    score_gate,
)
from dodeal_ai.units.call_intelligence.signals import interruptions, talk_share
from dodeal_ai.units.call_intelligence.transcriber import Transcript

OBJECTIONS = "objections"
SCORE = "score"


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
    eligible: bool,
) -> Wave2:
    """Wave 2 for one transcript. JobGone when stage 2 stopped under it.
    `eligible` is the call's eligibility for full analysis, as it is now."""
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
    await _score(run, wave, call, config, scope, settings, eligible)
    return wave


async def _score(
    run: PassRun,
    wave: Wave2,
    call: CallText,
    config: CallsConfig,
    scope: TenantScope,
    settings: Settings,
    eligible: bool,
) -> None:
    """The score part, or null with why -- the pass unrun when it would be."""
    share = talk_share(call.segments, CLIENT)
    objections = wave.parts[OBJECTIONS]
    gate = score_gate(
        scoring_enabled=config.scoring_enabled,
        eligible=eligible,
        client_share=share,
        objections_answered=objections is not None,
    )
    if gate is not None:
        wave.parts[SCORE] = None
        wave.reasons[SCORE] = gate
        return
    # The gate passes only with both: a share, and the objections answered.
    assert share is not None and objections is not None
    try:
        checks, model = await run_pass(
            run,
            SCORE,
            ScoreChecks,
            lambda metered: ask_checks(metered, call, scope=scope, settings=settings),
        )
    except PassFailed as failed:
        wave.parts[SCORE] = None
        wave.reasons[SCORE] = str(failed)
        return
    wave.parts[SCORE] = score_call(
        checks,
        objections,
        client_share=share,
        agent_interruptions=interruptions(call.segments)[AGENT],
    )
    wave.models[SCORE] = model
