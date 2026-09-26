"""unit_b.roles (stage 1, before wave 1): which diarized voice is the agent,
and which language each side speaks.

A transcript labelled by the engine (speaker_N) names no one, and every label
but "agent" reads as the client (prompts.role_of). This pass reads the call as
the engine labelled it -- the first ROLE_SEGMENTS segments, and each voice's
LANGUAGE_SEGMENTS longest segments from anywhere in the call, each under its
own id (RolesView) -- and answers each label's role -- agent, client or
unclear -- quoting one of that voice's own segments; and call_languages: the
client's and the agent's language, one code each (language.CALL_LANGUAGES),
judged from the words, not the script, so a dialect heard only late in the
call is still heard.

THE MAPPING AND THE LANGUAGES ARE JUDGED APART (judge). A voice's quote is
verified when any segment carrying that voice's own label holds it, under
the quote check's matcher: the cited segment first, then the voice's others
in call order; the segment it is found in is stored. Every label in the
segments shown must be named once and no other.
- TWO VOICES: a verified role stands; one verified role gives the other
  label the other role. The answer is malformed (one reprompt, then the pass
  fails) only when no role is verified or both are verified alike.
- ANY OTHER COUNT, as before: every agent or client quote must be verified
  and an unclear one's quote, when given, must pass the quote check, or the
  answer is malformed.
- THE LANGUAGES never make the answer malformed. A side's language counts
  only when its quote passes the quote check in a segment of a voice holding
  that side's role; a side whose quote fails is kept null, and the call's
  languages then come from the script (language.py) while the mapping stays.

CODE DECIDES what the answer is worth. The mapping is applied -- each label
becomes agent or client, a voice first heard later a client -- only when it is
clear: exactly one agent, at least one client and no unclear. Otherwise, or
when the pass failed, the labels stay and the transcript is uncertain; more
than two voices make it uncertain too, mapped or not. Stage 1 carries the
answer as judged and what became of it. The languages are used only with the
mapping applied and neither side's quote failed (spoken); otherwise the
language falls back to script (language.py).

ONE VOICE on a call of SINGLE_VOICE_MIN_SECONDS or more is uncertain
(single_voice), whoever labelled it: the engine or a stereo channel. A sales
call that long has two sides; hearing one means one was lost.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_ROLES, task_ceiling
from dodeal_ai.units.call_intelligence.evidence import (
    MAX_QUOTE_WORDS,
    CallText,
    Errors,
    Quote,
    SegmentId,
    Strict,
    found_at,
    holds,
    own_words,
    quote_errors,
    quote_words,
    relocated,
)
from dodeal_ai.units.call_intelligence.language import (
    UNHEARD,
    CallLanguage,
    Spoken,
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
# And each voice's longest segments from the whole call, by words: where its
# dialect shows. More costs input tokens on every labelled call; fewer can
# miss a side that says little until late.
LANGUAGE_SEGMENTS = 8

# More voices than any sales call has.
MAX_SPEAKERS = 10

UNCLEAR = "unclear"

# Why code doubts a transcript's roles, fixed codes only; the first two are
# evidence.ROLES_NOT_APPLIED.
ROLES_FAILED = "roles_failed"
ROLES_UNCLEAR = "roles_unclear"
OVER_TWO = "speakers_over_two"
SINGLE_VOICE = "single_voice"
# Every reason above, rebuilt in code on each run over the engine's labels: a
# re-analysed transcript's own never carries over (A3).
ROLE_REASONS = frozenset({ROLES_FAILED, ROLES_UNCLEAR, OVER_TWO, SINGLE_VOICE})

# From this long (the call's duration), one voice heard is doubted: under it,
# a call may be one side's short message. Provisional, like the audio floors.
SINGLE_VOICE_MIN_SECONDS = 30


class SpeakerRole(Strict):
    """One voice's role, and the words that show it."""

    speaker: Annotated[str, Field(pattern=ENGINE_LABEL.pattern)]
    role: Literal["agent", "client", "unclear"]
    quote: Quote
    segment: SegmentId


class SideLanguage(Strict):
    """One side's language, and that side's words in it; null when unheard."""

    language: CallLanguage | None
    quote: Quote
    segment: SegmentId


_NOT_HEARD = SideLanguage(language=None, quote=None, segment=None)


class CallLanguages(Strict):
    """The client's language and the agent's."""

    client: SideLanguage
    agent: SideLanguage


# An answer kept before the languages existed reads back as neither heard.
_NONE_HEARD = CallLanguages(client=_NOT_HEARD, agent=_NOT_HEARD)

# The two sides, as call_languages names them and as a voice's role.
SIDES = (CLIENT, AGENT)


class Roles(Strict):
    """unit_b.roles' answer, exactly."""

    speakers: Annotated[list[SpeakerRole], Field(min_length=1, max_length=MAX_SPEAKERS)]
    call_languages: CallLanguages = _NONE_HEARD


def needs_roles(transcript: Transcript) -> bool:
    """Whether any voice carries the engine's label, not a role."""
    return any(ENGINE_LABEL.match(speaker) for speaker in transcript.speakers)


@dataclass(frozen=True, slots=True)
class RolesView:
    """What the pass is shown -- the indices of the segments, in call order --
    and the whole call its quotes are checked against."""

    call: CallText
    shown: tuple[int, ...]


def opening(transcript: Transcript, *, country_code: str) -> RolesView:
    """The first ROLE_SEGMENTS segments and each voice's LANGUAGE_SEGMENTS
    longest (by words, the earlier first on a tie), each once, in order."""
    segments = transcript.segments
    shown = set(range(min(ROLE_SEGMENTS, len(segments))))
    for voice in {segment.speaker for segment in segments}:
        own = [n for n, segment in enumerate(segments) if segment.speaker == voice]
        own.sort(key=lambda n: (-len(segments[n].text.split()), n))
        shown.update(own[:LANGUAGE_SEGMENTS])
    return RolesView(
        CallText.of(transcript, country_code=country_code), tuple(sorted(shown))
    )


def roles_data(view: RolesView) -> str:
    """The segments shown with the engine's labels, one per line, each under
    its own id in the call."""
    call = view.call
    lines = "\n".join(
        f"[{segment_id(n)} {clock(call.segments[n].start_s)} "
        f"{call.segments[n].speaker}] {one_line(call.shown[n])}"
        for n in view.shown
    )
    return f"TRANSCRIPT:\n{lines}"


# The other role, given to the one voice of two whose role no quote showed.
_OTHER = {AGENT: CLIENT, CLIENT: AGENT}


@dataclass(frozen=True, slots=True)
class Judged:
    """Code's word on a roles answer (module docstring): each label's role as
    decided, with its quote and segment where verified and null where not;
    what makes the answer malformed; each side's language as kept, and
    whether a side's language quote failed."""

    speakers: tuple[SpeakerRole, ...]
    verified: tuple[bool, ...]
    errors: tuple[tuple[str, str], ...]
    languages: CallLanguages
    languages_failed: bool


def _own(call: CallText, found: SpeakerRole) -> int | None:
    """Where a voice's quote is found among the segments with its own label:
    the cited one first, then the others in call order; None for no quote,
    an unclear voice, or no segment of that voice holding it."""
    if found.quote is None or found.role == UNCLEAR:
        return None
    own = [n for n, s in enumerate(call.segments) if s.speaker == found.speaker]
    cited = None if found.segment is None else call.index_of(found.segment)
    order = ([cited] if cited in own else []) + [n for n in own if n != cited]
    return next((n for n in order if holds(call, found.quote, n)), None)


def _said(call: CallText, found: SpeakerRole, at: int) -> str | None:
    """The transcript's own words a verified voice's quote matched at `at`."""
    return None if found.quote is None else own_words(call, found.quote, at)


def _own_errors(call: CallText, where: str, found: SpeakerRole) -> Errors:
    """Why an agent's or a client's quote is not verified; an unclear one's
    quote, when given, under the plain quote check."""
    if found.role == UNCLEAR:
        return quote_errors(call, where, found.quote, found.segment)
    if found.quote is None:
        return [(where, "quote_missing")]
    if _own(call, found) is not None:
        return []
    quoted = quote_words(found.quote)
    if not quoted or len(quoted) > MAX_QUOTE_WORDS:
        return [(where, "quote_length")]
    elsewhere = any(holds(call, found.quote, n) for n in range(len(call.segments)))
    return [(where, "quote_wrong_speaker" if elsewhere else "quote_not_in_segment")]


def _paired(speakers: Sequence[SpeakerRole], verified: Sequence[bool]) -> Errors:
    """Two voices: malformed only when no role is verified or both alike."""
    sure = [s.role for s, ok in zip(speakers, verified, strict=True) if ok]
    if not sure:
        return [("speakers", "no_role_verified")]
    if len(sure) == 2 and sure[0] == sure[1]:
        return [("speakers", "same_role")]
    return []


def _decided(speakers: Sequence[SpeakerRole], verified: Sequence[bool]) -> list[str]:
    """Two voices, one verified: the other takes the other role."""
    sure = [s.role for s, ok in zip(speakers, verified, strict=True) if ok]
    if len(speakers) != 2 or len(sure) != 1:
        return [s.role for s in speakers]
    return [s.role if ok else _OTHER[sure[0]] for s, ok in zip(speakers, verified)]


def _languages(
    call: CallText, answer: Roles, speakers: Sequence[SpeakerRole]
) -> tuple[CallLanguages, bool]:
    """Each side's language kept when its quote passes the quote check in a
    segment of a voice holding that side's role (as decided); a side whose
    quote fails is null, and failed is true."""
    kept: dict[str, SideLanguage] = {}
    failed = False
    for side in SIDES:
        found: SideLanguage = getattr(answer.call_languages, side)
        if found.language is None:
            kept[side] = _NOT_HEARD
            continue
        voices = {s.speaker for s in speakers if s.role == side}
        at = (
            None
            if found.quote is None or found.segment is None
            else found_at(call, found.quote, found.segment)
        )
        if at is None or call.segments[at].speaker not in voices:
            kept[side], failed = _NOT_HEARD, True
        else:
            kept[side] = found.model_copy(update={"segment": segment_id(at)})
    return CallLanguages(client=kept[CLIENT], agent=kept[AGENT]), failed


def judge(view: RolesView, answer: Roles) -> Judged:
    """The answer judged as the module docstring says."""
    call = view.call
    heard = sorted({call.segments[n].speaker for n in view.shown})
    errors: Errors = []
    if sorted(found.speaker for found in answer.speakers) != heard:
        errors.append(("speakers", "not_each_once"))
    found = [_own(call, speaker) for speaker in answer.speakers]
    verified = [at is not None for at in found]
    if len(heard) == 2:
        paired = [] if errors else _paired(answer.speakers, verified)
        for n, speaker in enumerate(answer.speakers):
            if paired and not verified[n]:
                errors += _own_errors(call, f"speakers.{n}", speaker)
        errors += paired
    else:
        for n, speaker in enumerate(answer.speakers):
            errors += _own_errors(call, f"speakers.{n}", speaker)
    roles = _decided(answer.speakers, verified)
    speakers = tuple(
        speaker.model_copy(
            update={
                "role": role,
                "quote": None if at is None else _said(call, speaker, at),
                "segment": None if at is None else segment_id(at),
            }
        )
        for speaker, role, at in zip(answer.speakers, roles, found, strict=True)
    )
    languages, failed = _languages(call, answer, speakers)
    return Judged(speakers, tuple(verified), tuple(errors), languages, failed)


def check_roles(view: RolesView) -> Callable[[Roles], None]:
    """The rules the schema cannot hold (module docstring), for call_model."""

    def check(answer: Roles) -> None:
        errors = judge(view, answer).errors
        if errors:
            raise output_rejected(ROLES_LABEL, errors)

    return check


def stored(view: RolesView, answer: Roles) -> Roles:
    """The answer as kept: a verified voice's quote the transcript's own words
    at the segment it is found in, each language quote's likewise (relocated);
    nothing else changes, so judging it again gives the same word."""
    call = view.call
    speakers = []
    for speaker in answer.speakers:
        at = _own(call, speaker)
        if at is not None:
            moved = {"segment": segment_id(at), "quote": _said(call, speaker, at)}
            speaker = speaker.model_copy(update=moved)
        speakers.append(speaker)
    languages = relocated(call, answer.call_languages)
    return answer.model_copy(update={"speakers": speakers, "call_languages": languages})


def spoken(judged: Judged | None, *, applied: bool) -> Spoken:
    """Each side's language, only from an answer whose mapping was applied
    and whose language quotes all held; else unheard (the script decides)."""
    if judged is None or not applied or judged.languages_failed:
        return UNHEARD
    return Spoken(
        client=judged.languages.client.language,
        agent=judged.languages.agent.language,
    )


async def ask_roles(
    client: LLMClient, view: RolesView, *, scope: TenantScope, settings: Settings
) -> tuple[Roles, LLMResponse]:
    """unit_b.roles: one call, or two when the first answer is malformed."""
    answer, response = await call_model(
        client,
        build_call_prompt(ROLES_TEMPLATE, roles_data(view)),
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
        check=check_roles(view),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=REPROMPT_TAILS,
    )
    return stored(view, answer), response


def single_voice(transcript: Transcript, call_seconds: float) -> tuple[str, ...]:
    """(single_voice,) for one voice heard on a call long enough to have two."""
    alone = len(transcript.speakers) == 1
    return (SINGLE_VOICE,) if alone and call_seconds >= SINGLE_VOICE_MIN_SECONDS else ()


def without(transcript: Transcript, reasons: frozenset[str]) -> Transcript:
    """The transcript with none of `reasons` among its uncertain reasons."""
    kept = tuple(r for r in transcript.uncertain_reasons if r not in reasons)
    return Transcript.of(
        transcript.segments,
        provider=transcript.provider,
        model=transcript.model,
        reasons=kept,
    )


def apply_roles(
    transcript: Transcript, judged: Judged | None, *, call_seconds: float
) -> tuple[Transcript, dict[str, object]]:
    """The transcript relabelled when the judged mapping is clear, doubted
    when it is not, failed (None), over two voices or one voice on a long
    call -- every roles reason it came with dropped first and rebuilt here --
    and stage 1's block: each voice as judged, with verified."""
    roles: list[str] = (
        [] if judged is None or judged.errors else [s.role for s in judged.speakers]
    )
    clear = roles.count(AGENT) == 1 and CLIENT in roles and UNCLEAR not in roles
    mapping = {} if judged is None else {s.speaker: s.role for s in judged.speakers}
    reasons = [
        *([OVER_TWO] if len(transcript.speakers) > 2 else []),
        *single_voice(transcript, call_seconds),
        *([ROLES_FAILED] if judged is None else []),
        *([ROLES_UNCLEAR] if judged is not None and not clear else []),
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
        reasons=without(transcript, ROLE_REASONS).uncertain_reasons,
    ).doubted(*reasons)
    block: dict[str, object] = {
        "speakers": None
        if judged is None
        else [
            {**found.model_dump(), "verified": ok}
            for found, ok in zip(judged.speakers, judged.verified, strict=True)
        ],
        "applied": clear,
        "reasons": reasons,
        "call_languages": None if judged is None else judged.languages.model_dump(),
    }
    return relabelled, block
