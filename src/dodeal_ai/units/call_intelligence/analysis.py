"""Wave 1 (Unit B): what stage 1 says about a transcribed call, beside the
transcript -- the language and the two model passes (the analysis), and the
signals, numbers, alarm phrases and escalations found in code (the signals).

THE SIGNALS BLOCK IS ALWAYS THERE for a transcript: it is found in code before
any pass starts and goes out top-level in stage 1, whatever becomes of the
passes -- so a model outage still delivers an off_channel_contact escalation.

EVERY TRANSCRIPT GETS IT: a call from min_transcribe_seconds up (BRD B8), and
an uncertain one too, with every field marked uncertain (passes.settled).

NOTHING RECEIVED IS PAID FOR TWICE: each pass is kept, counted and started at
most twice (paid.py).

ROLES FIRST: a transcript the engine labelled (speaker_N) goes through the
roles pass before anything reads agent or client (roles.py); everything after
it, stage 1's transcript included, reads the transcript it returns, and the
summary language follows the languages it heard, else the script
(language.py). Stage 1's languages block says which.

A PASS THAT FAILS still lets stage 1 go: analysis is null and analysis_reason
says which pass and why (`extract_model_unavailable`, `prose_malformed_output`,
`extract_pass_interrupted`, or `llm_not_configured` for a run with no client).
The prose pass is not started once the extraction has failed.
"""

from __future__ import annotations

from dataclasses import dataclass

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.jobs import Job, start_pass
from dodeal_ai.core.llm import LLMClient
from dodeal_ai.units.call_intelligence.alarms import alarms_if_enabled
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.keywords import spot_keywords
from dodeal_ai.units.call_intelligence.language import (
    UNHEARD,
    Spoken,
    languages_block,
)
from dodeal_ai.units.call_intelligence.numbers import numbers_if_enabled
from dodeal_ai.units.call_intelligence.paid import (
    PassFailed,
    PassRun,
    PassUsage,
    Stamp,
    run_pass,
)
from dodeal_ai.units.call_intelligence.passes import (
    CallText,
    Extraction,
    Prose,
    extract,
    settled,
    write_prose,
)
from dodeal_ai.units.call_intelligence.prompts import PROMPT_SET_VERSION
from dodeal_ai.units.call_intelligence.roles import (
    Roles,
    apply_roles,
    ask_roles,
    needs_roles,
    opening,
    single_voice,
    spoken,
)
from dodeal_ai.units.call_intelligence.signals import SIGNALS_VERSION, call_signals
from dodeal_ai.units.call_intelligence.transcriber import Transcript

ROLES = "roles"
EXTRACT = "extract"
PROSE = "prose"
NO_CLIENT = "llm_not_configured"


@dataclass(frozen=True, slots=True)
class Wave1:
    """Stage 1's analysis, or null with the reason; the signals block found in
    code, never null; the versions both ran on; the transcript as the roles
    left it, the roles block (None when no voice needed one), and the
    languages block (language.languages_block)."""

    analysis: dict[str, object] | None
    reason: str | None
    versions: dict[str, object]
    signals: dict[str, object]
    transcript: Transcript
    roles: dict[str, object] | None
    languages: dict[str, object]


def _analysis(
    call: CallText, extraction: Extraction, prose: Prose
) -> dict[str, object]:
    """The analysis block: the passes' answers with code's word on evidence and
    certainty (passes.settled)."""
    kept = settled(extraction, call)
    return {
        "language": call.language,
        "uncertain": call.uncertain,
        "summary": prose.summary,
        "elements": {
            name: kept[name]
            for name in (
                "wanted",
                "discussed",
                "concerns",
                "agreed",
                "next_step",
                "ending",
            )
        },
        "details": kept["details"],
        "mood": kept["mood"],
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
        # The tenant's vocabulary spotted in code; None when it listed none.
        "keywords": (
            spot_keywords(segments, config.keyword_vocabulary)
            if config.keyword_vocabulary
            else None
        ),
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
    stamps: dict[str, Stamp] = {}
    run = (
        None
        if client is None
        else PassRun(job, work, config.result_ttl_seconds, client, usage, start_pass)
    )
    seconds = int(str(job.metadata["duration_seconds"]))
    transcript, roles, heard = await _roles(
        run, transcript, seconds, config, scope, settings, stamps
    )
    languages = languages_block(transcript, heard)
    call = CallText.of(transcript, country_code=config.phone_country_code, spoken=heard)
    code, digest = _found_in_code(transcript, config, job)
    versions: dict[str, object] = {
        "prompt": PROMPT_SET_VERSION,
        "signals": SIGNALS_VERSION,
        "model": None,
        "transcriber": f"{transcript.provider}/{transcript.model}",
        "alarm_list_digest": digest,
        "passes": {},
    }
    if run is None:
        return Wave1(None, NO_CLIENT, versions, code, transcript, roles, languages)
    try:
        extraction, stamps[EXTRACT] = await run_pass(
            run,
            EXTRACT,
            Extraction,
            lambda metered: extract(metered, call, scope=scope, settings=settings),
        )
        doubted = settled(extraction, call)
        prose, stamps[PROSE] = await run_pass(
            run,
            PROSE,
            Prose,
            lambda metered: write_prose(
                metered, call, doubted, scope=scope, settings=settings
            ),
        )
    except PassFailed as failed:
        _stamp(versions, stamps)
        return Wave1(None, str(failed), versions, code, transcript, roles, languages)
    _stamp(versions, stamps)
    analysis = _analysis(call, extraction, prose)
    return Wave1(analysis, None, versions, code, transcript, roles, languages)


async def _roles(
    run: PassRun | None,
    transcript: Transcript,
    call_seconds: int,
    config: CallsConfig,
    scope: TenantScope,
    settings: Settings,
    stamps: dict[str, Stamp],
) -> tuple[Transcript, dict[str, object] | None, Spoken]:
    """The transcript with its voices' roles, the roles block, and each side's
    language as heard; as it came, None and unheard, when no voice carries the
    engine's label -- doubted even then when one voice is all a long call has."""
    if not needs_roles(transcript):
        doubted = transcript.doubted(*single_voice(transcript, call_seconds))
        return doubted, None, UNHEARD
    answer: Roles | None = None
    if run is not None:
        head = opening(transcript, country_code=config.phone_country_code)
        try:
            answer, stamps[ROLES] = await run_pass(
                run,
                ROLES,
                Roles,
                lambda metered: ask_roles(
                    metered, head, scope=scope, settings=settings
                ),
            )
        except PassFailed:
            answer = None
    relabelled, block = apply_roles(transcript, answer, call_seconds=call_seconds)
    return relabelled, block, spoken(answer, applied=block["applied"] is True)


def _stamp(versions: dict[str, object], stamps: dict[str, Stamp]) -> None:
    """The models wave 1 ran on, joined, and each pass's provider and model."""
    models = sorted({stamp.model for stamp in stamps.values()})
    versions["model"] = ",".join(models) or None
    versions["passes"] = {name: stamp.to_dict() for name, stamp in stamps.items()}
