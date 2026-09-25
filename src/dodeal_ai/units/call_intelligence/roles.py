"""unit_b.roles (stage 1, before wave 1): which diarized voice is the agent.

A transcript labelled by the engine (speaker_N) names no one, and every label
but "agent" reads as the client (prompts.role_of). This pass reads the first
ROLE_SEGMENTS segments as the engine labelled them and answers each label's
role -- agent, client or unclear -- quoting one of that voice's own segments.

THE CHECKS, in code, any failure a malformed answer (one reprompt, then the
pass fails): every label in those segments named once and no other; an agent
or a client quoted, under the quote check, from that voice's own segment.

CODE DECIDES what the answer is worth. The mapping is applied -- each label
becomes agent or client, a voice first heard later a client -- only when it is
clear: exactly one agent, at least one client and no unclear. Otherwise, or
when the pass failed, the labels stay and the transcript is uncertain; more
than two voices make it uncertain too, mapped or not. Stage 1 carries the
answer and what became of it.

ONE VOICE on a call of SINGLE_VOICE_MIN_SECONDS or more is uncertain
(single_voice), whoever labelled it: the engine or a stereo channel. A sales
call that long has two sides; hearing one means one was lost.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_ROLES, task_ceiling
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Errors,
    Quote,
    SegmentId,
    Strict,
    evidence_errors,
    quote_errors,
)
from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    CLIENT,
    ENGINE_LABEL,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    ROLES_TEMPLATE,
    build_call_prompt,
    clock,
    one_line,
    segment_id,
)
from dodeal_ai.units.call_intelligence.transcriber import Transcript
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

ROLES_LABEL = "llm.unit_b.roles"

# What the answer may cost: a role and a short quote per voice, on a
# non-reasoning model; the larger when the profile reasons (item 116).
ROLES_MAX_OUTPUT_TOKENS = 800
ROLES_REASONING_MAX_OUTPUT_TOKENS = 2000

# The segments the pass reads: an opening names who is calling whom.
ROLE_SEGMENTS = 20

# More voices than any sales call has.
MAX_SPEAKERS = 10

UNCLEAR = "unclear"

# Why code doubts a transcript's roles, fixed codes only.
ROLES_FAILED = "roles_failed"
ROLES_UNCLEAR = "roles_unclear"
OVER_TWO = "speakers_over_two"
SINGLE_VOICE = "single_voice"

# From this long (the call's duration), one voice heard is doubted: under it,
# a call may be one side's short message. Provisional, like the audio floors.
SINGLE_VOICE_MIN_SECONDS = 30


class SpeakerRole(Strict):
    """One voice's role, and the words that show it."""

    speaker: Annotated[str, Field(pattern=ENGINE_LABEL.pattern)]
    role: Literal["agent", "client", "unclear"]
    quote: Quote
    segment: SegmentId


class Roles(Strict):
    """unit_b.roles' answer, exactly."""

    speakers: Annotated[list[SpeakerRole], Field(min_length=1, max_length=MAX_SPEAKERS)]


def needs_roles(transcript: Transcript) -> bool:
    """Whether any voice carries the engine's label, not a role."""
    return any(ENGINE_LABEL.match(speaker) for speaker in transcript.speakers)


def opening(transcript: Transcript, *, country_code: str) -> CallText:
    """The first ROLE_SEGMENTS segments, as the pass reads them."""
    head = Transcript.of(
        transcript.segments[:ROLE_SEGMENTS],
        provider=transcript.provider,
        model=transcript.model,
    )
    return CallText.of(head, country_code=country_code)


def roles_data(call: CallText) -> str:
    """The opening with the engine's labels, one segment per line."""
    lines = "\n".join(
        f"[{segment_id(n)} {clock(segment.start_s)} {segment.speaker}] {one_line(text)}"
        for n, (segment, text) in enumerate(zip(call.segments, call.shown, strict=True))
    )
    return f"TRANSCRIPT:\n{lines}"


def check_roles(call: CallText) -> Callable[[Roles], None]:
    """The rules the schema cannot hold (module docstring), for call_model."""
    heard = sorted({segment.speaker for segment in call.segments})

    def check(answer: Roles) -> None:
        errors: Errors = []
        if sorted(found.speaker for found in answer.speakers) != heard:
            errors.append(("speakers", "not_each_once"))
        for n, found in enumerate(answer.speakers):
            where = f"speakers.{n}"
            if found.role == UNCLEAR:
                errors += quote_errors(call, where, found.quote, found.segment)
                continue
            owed = evidence_errors(call, where, found.quote, found.segment)
            errors += owed
            index = None if found.segment is None else call.index_of(found.segment)
            wrong = index is not None and call.segments[index].speaker != found.speaker
            if not owed and wrong:
                errors.append((where, "quote_wrong_speaker"))
        if errors:
            raise output_rejected(ROLES_LABEL, tuple(errors))

    return check


async def ask_roles(
    client: LLMClient, call: CallText, *, scope: TenantScope, settings: Settings
) -> tuple[Roles, LLMResponse]:
    """unit_b.roles: one call, or two when the first answer is malformed."""
    return await call_model(
        client,
        build_call_prompt(ROLES_TEMPLATE, roles_data(call)),
        Roles,
        ROLES_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_B_ROLES,
        max_output_tokens=task_ceiling(
            settings,
            PROFILE_UNIT_B_ROLES,
            plain=ROLES_MAX_OUTPUT_TOKENS,
            reasoning=ROLES_REASONING_MAX_OUTPUT_TOKENS,
            client=client,
        ),
        check=check_roles(call),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=REPROMPT_TAILS,
    )


def single_voice(transcript: Transcript, call_seconds: float) -> tuple[str, ...]:
    """(single_voice,) for one voice heard on a call long enough to have two."""
    alone = len(transcript.speakers) == 1
    return (SINGLE_VOICE,) if alone and call_seconds >= SINGLE_VOICE_MIN_SECONDS else ()


def apply_roles(
    transcript: Transcript, answer: Roles | None, *, call_seconds: float
) -> tuple[Transcript, dict[str, object]]:
    """The transcript relabelled when the answer's mapping is clear, doubted
    when it is not, failed (None), over two voices or one voice on a long
    call; and stage 1's block."""
    mapping = {} if answer is None else {s.speaker: s.role for s in answer.speakers}
    roles: list[str] = list(mapping.values())
    clear = roles.count(AGENT) == 1 and CLIENT in roles and UNCLEAR not in roles
    reasons = [
        *([OVER_TWO] if len(transcript.speakers) > 2 else []),
        *single_voice(transcript, call_seconds),
        *([ROLES_FAILED] if answer is None else []),
        *([ROLES_UNCLEAR] if answer is not None and not clear else []),
    ]
    segments = (
        tuple(
            segment.model_copy(update={"speaker": mapping.get(segment.speaker, CLIENT)})
            for segment in transcript.segments
        )
        if clear
        else transcript.segments
    )
    relabelled = Transcript.of(
        segments,
        provider=transcript.provider,
        model=transcript.model,
        reasons=transcript.uncertain_reasons,
    ).doubted(*reasons)
    block: dict[str, object] = {
        "speakers": None
        if answer is None
        else [found.model_dump() for found in answer.speakers],
        "applied": clear,
        "reasons": reasons,
    }
    return relabelled, block
