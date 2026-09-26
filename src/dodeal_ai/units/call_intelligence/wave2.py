"""Wave 2 (Unit B): the stage-2 passes on a done call's transcript, one after
another, each on its own profile.

  objections  unit_b.objections  the client's objections (objections.py)
  score       unit_b.score       yes-or-no checks, marked in code (score.py);
                                 not run at all when the call gets no score
  escalations unit_b.escalations the five BRD issues, merged with stage 1's
                                 off_channel_contact (escalations.py)
  coaching    unit_b.coaching    observations, moments, a plan and the call's
                                 stages, tone-checked in code (coaching.py)
  extras      unit_b.extras      keywords, tags, a WhatsApp suggestion never
                                 sent by us, and seriousness, banded in code
                                 (extras.py)

EACH PASS STANDS ALONE. Its answer becomes its part; a pass that fails --
twice unanswered, or malformed after its one reprompt -- leaves its part null
with a reason (`objections_malformed_output`), and the passes after it still
run. Every pass is kept, counted and started at most twice (paid.py), so a
re-run of stage 2 pays for nothing it already received.

The transcript is read as wave 1 read it: the prompt copy, masked under the
tenant's country code, the summary language decided in code.

THE STAGE-2 RESULT (stage2_result), held in db3 and delivered as call.stage2:
each pass's part, null where the pass failed or did not run, the reason for
each null, and the versions -- the prompt set, the objection list, the rubric,
the tone list, and the model each pass's answer came from.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.jobs import Job, start_stage2_pass
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.units.call_intelligence.coaching import (
    TONE_LIST_VERSION,
    Coaching,
    coach,
    coaching_part,
)
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.escalations import (
    Flags,
    escalations_part,
    find_flags,
)
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.extras import Extras, extras_part, find_extras
from dodeal_ai.units.call_intelligence.objections import (
    OBJECTION_LIST_VERSION,
    Objections,
    find_objections,
    objections_part,
)
from dodeal_ai.units.call_intelligence.paid import (
    PassFailed,
    PassRun,
    PassUsage,
    Stamp,
    run_pass,
)
from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    CLIENT,
    PROMPT_SET_VERSION,
)
from dodeal_ai.units.call_intelligence.score import (
    RUBRIC_VERSION,
    ScoreChecks,
    ask_checks,
    score_call,
    score_gate,
)
from dodeal_ai.units.call_intelligence.signals import interruptions, talk_share
from dodeal_ai.units.call_intelligence.transcriber import Transcript

OBJECTIONS = "objections"
SCORE = "score"
ESCALATIONS = "escalations"
COACHING = "coaching"
EXTRAS = "extras"
PASS_NAMES = (OBJECTIONS, SCORE, ESCALATIONS, COACHING, EXTRAS)


@dataclass(slots=True)
class Wave2:
    """Each pass's part, or None with its reason; and the model per pass."""

    parts: dict[str, dict[str, object] | None] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    stamps: dict[str, Stamp] = field(default_factory=dict)


def stage2_result(job: Job, wave: Wave2) -> dict[str, object]:
    """The stage-2 result: every part, why each null one is null, and the
    versions it ran on."""
    return {
        "stage": 2,
        "call_id": job.call_id,
        **{name: wave.parts.get(name) for name in PASS_NAMES},
        "reasons": dict(wave.reasons),
        "versions": {
            "prompt": PROMPT_SET_VERSION,
            "objection_list": OBJECTION_LIST_VERSION,
            "rubric": RUBRIC_VERSION,
            "tone_list": TONE_LIST_VERSION,
            "model": dict(wave.models),
            "passes": {name: stamp.to_dict() for name, stamp in wave.stamps.items()},
        },
    }


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
    stage1_escalations: Sequence[dict[str, object]],
) -> Wave2:
    """Wave 2 for one transcript. JobGone when stage 2 stopped under it.
    `eligible` is the call's eligibility for full analysis, as it is now;
    `stage1_escalations` are the ones stage 1 found in code."""
    call = CallText.of(transcript, country_code=config.phone_country_code)
    run = PassRun(
        job, work, config.result_ttl_seconds, client, usage, start_stage2_pass
    )
    wave = Wave2()
    found = await _part(
        run,
        wave,
        OBJECTIONS,
        Objections,
        lambda metered: find_objections(metered, call, scope=scope, settings=settings),
    )
    wave.parts[OBJECTIONS] = None if found is None else objections_part(found)
    await _score(run, wave, call, config, scope, settings, eligible)
    flags = await _part(
        run,
        wave,
        ESCALATIONS,
        Flags,
        lambda metered: find_flags(metered, call, scope=scope, settings=settings),
    )
    wave.parts[ESCALATIONS] = (
        None if flags is None else escalations_part(call, flags, stage1_escalations)
    )
    coached = await _part(
        run,
        wave,
        COACHING,
        Coaching,
        lambda metered: coach(metered, call, scope=scope, settings=settings),
    )
    wave.parts[COACHING] = None if coached is None else coaching_part(call, coached)
    extras = await _part(
        run,
        wave,
        EXTRAS,
        Extras,
        lambda metered: find_extras(
            metered,
            call,
            vocabulary=config.keyword_vocabulary,
            default_dialect=config.whatsapp_default_dialect,
            scope=scope,
            settings=settings,
        ),
    )
    wave.parts[EXTRAS] = (
        None
        if extras is None
        else extras_part(call, extras, config.whatsapp_default_dialect)
    )
    return wave


async def _part[M: BaseModel](
    run: PassRun,
    wave: Wave2,
    name: str,
    schema: type[M],
    call: Callable[[LLMClient], Awaitable[tuple[M, LLMResponse]]],
) -> M | None:
    """One pass's answer, its model noted; None, the part null with why, when
    it has failed."""
    try:
        answer, stamp = await run_pass(run, name, schema, call)
    except PassFailed as failed:
        wave.parts[name] = None
        wave.reasons[name] = str(failed)
        return None
    wave.models[name] = stamp.model
    wave.stamps[name] = stamp
    return answer


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
    checks = await _part(
        run,
        wave,
        SCORE,
        ScoreChecks,
        lambda metered: ask_checks(metered, call, scope=scope, settings=settings),
    )
    if checks is not None:
        wave.parts[SCORE] = score_call(
            checks,
            objections,
            client_share=share,
            agent_interruptions=interruptions(call.segments)[AGENT],
        )
