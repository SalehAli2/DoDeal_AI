"""unit_b.coaching (wave 2): coaching for the agent, in the summary language.

  observations  two or three, at least one strength and one improvement, each
                quoting the segment it rests on; every improvement with a
                "say it like this"
  moments       up to four: the segment, what happened, what would be better;
                its timestamp is the segment's start, read in code
  plan          a development plan of exactly three actions
  stages        opening, rapport, discovery, qualification, presentation,
                objections and close: each done yes or no, a yes quoted
  ask_why       code's, not the model's: a fixed tip to ask the client why,
                when stage 1's loss reason is no_reason_given; else null

WHAT THE PASS IS TOLD BESIDE THE CALL (coaching_data, D-63), decided in code:
the agent's dialect, from the roles pass's call_languages.agent, msa_ar when
unheard or not Arabic, which every Arabic "say it like this" is written in;
and stage 1's next step, booked and its kind, so a booked call is never told
to confirm or set one (a prompt rule, not checked in code).

THE CHECKS, all in code. Two refuse the whole answer (one reprompt, then the
pass fails and the coaching part is null): the words written at least 60 % in
the summary language's script, quotes left out (evidence.in_language); and
THE TONE CHECK (BRD P6) -- no phrase of tone_list_v1, absolute or harsh, in
English or Arabic, matched as the alarm phrases are (alarms.py: folded,
proclitics, two-word gaps).

THE REST IS KEPT ITEM BY ITEM (D-101). An observation stands when its quote
holds and, for an improvement, it has a way to say it; the part carries only
those. A stage done yes whose quote fails stays yes with its quote and
segment null and unverified true; a quote given for a stage done no and
failing is dropped. A moment on a segment that does not exist is dropped.
The answer earns the one reprompt only when no verified strength or no
verified improvement is left, or more than half its quotes fail; then the
second answer's verified observations and stages fill what the first's lack
(merged_coaching), and coaching with still no verified strength or
improvement fails the pass. The counts dropped and unverified go on the
stage-2 outcome line, as the extras' do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, get_args

from pydantic import Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.errors import MalformedOutputError
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_COACHING, task_ceiling
from dodeal_ai.units.call_intelligence.alarms import phrase_in, words
from dodeal_ai.units.call_intelligence.evidence import (
    Answer,
    CallText,
    Cited,
    Errors,
    Quote,
    Said,
    SegmentId,
    evidence_errors,
    failed_quotes,
    in_language,
    mostly_failed,
    quote_errors,
    relocated,
)
from dodeal_ai.units.call_intelligence.language import ARABIC_LANGUAGES, Spoken
from dodeal_ai.units.call_intelligence.passes import NextKind
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    build_call_prompt,
    clock,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    Mend,
    call_model,
    output_rejected,
)

COACHING_LABEL = "llm.unit_b.coaching"

# What the answer may cost (register item 15): three observations, four
# moments, three actions and seven stages, sized against Arabic coaching on a
# non-reasoning model.
COACHING_MAX_OUTPUT_TOKENS = 2000
# The same answer on a reasoning profile, whose hidden reasoning is spent
# inside the ceiling (register item 116): the ceiling follows the profile.
COACHING_REASONING_MAX_OUTPUT_TOKENS = 4000

# The tone list (BRD P6): absolute or harsh words a coach does not use. Small
# on purpose -- a match costs a reprompt -- and versioned: a change is a new
# version, and stage 2 carries the one it ran on.
TONE_LIST_VERSION = "tone_list_v1"
HARSH_PHRASES: tuple[str, ...] = (
    "you always",
    "you never",
    "terrible",
    "awful",
    "useless",
    "incompetent",
    "pathetic",
    "stupid",
    "lazy",
    "hopeless",
    "clueless",
    "unacceptable",
    "أنت دائما",
    "أنت أبدا",
    "فاشل",
    "غبي",
    "كسول",
    "سيء جدا",
    "كارثي",
    "عديم الفائدة",
    "مخزي",
    "غير مقبول",
)

# The tip stage 2 adds when stage 1 found a dead call with no reason given
# (passes.py, loss_reason no_reason_given): fixed words in the summary
# language, never a model's, so they pass the tone list as written.
NO_REASON_GIVEN = "no_reason_given"
ASK_WHY: dict[str, str] = {
    "en": (
        "The client said no without giving a reason. Before the call ends, "
        "ask what is holding them back: the answer shapes the next call."
    ),
    "ar": (
        "رفض العميل دون أن يذكر سببا. قبل إنهاء المكالمة، اسأله عما يمنعه: "
        "جوابه يحدد المكالمة القادمة."
    ),
}

# The dialect a "say it like this" is written in when the agent's language is
# unheard or not Arabic: Modern Standard Arabic, understood everywhere.
MSA = "msa_ar"
_NEXT_KINDS = frozenset(get_args(NextKind.__value__))


@dataclass(frozen=True, slots=True)
class NextStepSeen:
    """Stage 1's next step as coaching is told it: booked, and its kind."""

    booked: bool = False
    kind: str | None = None


NO_NEXT_STEP = NextStepSeen()

STRENGTH = "strength"
IMPROVEMENT = "improvement"
STAGE_NAMES = (
    "opening",
    "rapport",
    "discovery",
    "qualification",
    "presentation",
    "objections",
    "close",
)

# Lengths past any honest line of coaching.
_TEXT_CHARS = 400

type _Text = Annotated[str, Field(min_length=1, max_length=_TEXT_CHARS)]


class Observation(Answer):
    """A strength or an improvement, the quote it rests on, and for an
    improvement, the words to use instead."""

    kind: Literal["strength", "improvement"]
    text: _Text
    quote: Said
    segment: Cited
    say_it_like_this: Annotated[str | None, Field(max_length=_TEXT_CHARS)]


class Moment(Answer):
    """A moment on the call, and what would have been better."""

    segment: Cited
    what_happened: _Text
    better: _Text


class Stage(Answer):
    """One stage of the call: done or not, a yes quoted."""

    done: Literal["yes", "no"]
    quote: Quote
    segment: SegmentId


class Stages(Answer):
    opening: Stage
    rapport: Stage
    discovery: Stage
    qualification: Stage
    presentation: Stage
    objections: Stage
    close: Stage


class Coaching(Answer):
    """unit_b.coaching's answer, exactly."""

    observations: Annotated[list[Observation], Field(min_length=2, max_length=3)]
    moments: Annotated[list[Moment], Field(max_length=4)]
    plan: Annotated[list[_Text], Field(min_length=3, max_length=3)]
    stages: Stages


_HARSH = tuple(words(phrase) for phrase in HARSH_PHRASES)


def harsh(text: str) -> bool:
    """Whether `text` holds a phrase of the tone list."""
    said = words(text)
    return any(phrase_in(phrase, said) for phrase in _HARSH)


def _written(answer: Coaching) -> list[tuple[str, str]]:
    """Every free text the coaching writes, where it is: never a quote."""
    texts: list[tuple[str, str]] = []
    for n, seen in enumerate(answer.observations):
        texts.append((f"observations.{n}", seen.text))
        if seen.say_it_like_this is not None:
            texts.append((f"observations.{n}.say_it_like_this", seen.say_it_like_this))
    for n, moment in enumerate(answer.moments):
        texts += [
            (f"moments.{n}.what_happened", moment.what_happened),
            (f"moments.{n}.better", moment.better),
        ]
    texts += [(f"plan.{n}", action) for n, action in enumerate(answer.plan)]
    return texts


def _seen_errors(call: CallText, where: str, seen: Observation) -> Errors:
    """Why an observation does not stand: its quote, or an improvement with
    no way to say it; [] when it stands."""
    errors = evidence_errors(call, where, seen.quote, seen.segment)
    if seen.kind == IMPROVEMENT and seen.say_it_like_this is None:
        errors.append((where, "say_it_like_this_missing"))
    return errors


def _stage_errors(call: CallText, where: str, stage: Stage) -> Errors:
    """A stage's quote check: owed on a yes, checked when given on a no."""
    owed = evidence_errors if stage.done == "yes" else quote_errors
    return owed(call, where, stage.quote, stage.segment)


def coaching_quotes(call: CallText, answer: Coaching) -> dict[str, Errors]:
    """Every observation, and every stage owing or giving a quote, by where
    it is, with why it does not stand ([] for one that does)."""
    found = {
        f"observations.{n}": _seen_errors(call, f"observations.{n}", seen)
        for n, seen in enumerate(answer.observations)
    }
    for name in STAGE_NAMES:
        stage: Stage = getattr(answer.stages, name)
        if stage.done == "yes" or (stage.quote, stage.segment) != (None, None):
            where = f"stages.{name}"
            found[where] = _stage_errors(call, where, stage)
    return found


def _standing(call: CallText, answer: Coaching) -> list[Observation]:
    """The observations that stand, in order."""
    return [
        seen
        for n, seen in enumerate(answer.observations)
        if not _seen_errors(call, f"observations.{n}", seen)
    ]


def coaching_failures(call: CallText, answer: Coaching) -> Errors:
    """What a second answer may make up for (D-101): no verified strength,
    no verified improvement, or more than half the quotes failing; then
    every failure, so the reprompt lists them. [] keeps the answer."""
    kinds = {seen.kind for seen in _standing(call, answer)}
    missing: Errors = [
        ("observations", f"no_{kind}")
        for kind in (STRENGTH, IMPROVEMENT)
        if kind not in kinds
    ]
    quotes = coaching_quotes(call, answer)
    if missing or mostly_failed(quotes):
        return missing + failed_quotes(quotes)
    return []


def merged_coaching(call: CallText) -> Callable[[Coaching, Coaching], Coaching]:
    """The first answer, filled from the second (D-101): the first's standing
    observations, then the second's first standing one of each kind the first
    lacks, then -- to keep two -- the first's others, three at most; and each
    stage whose quote failed replaced by the second's where that one holds.
    Moments and the plan stay the first's."""

    def merge(first: Coaching, second: Coaching) -> Coaching:
        kept = _standing(call, first)
        kinds = {seen.kind for seen in kept}
        for seen in _standing(call, second):
            if seen.kind not in kinds:
                kept.append(seen)
                kinds.add(seen.kind)
        for seen in first.observations:
            if len(kept) < 2 and seen not in kept:
                kept.append(seen)
        stages = {
            name: getattr(second.stages, name)
            for name in STAGE_NAMES
            if _stage_errors(call, name, getattr(first.stages, name))
            and not _stage_errors(call, name, getattr(second.stages, name))
        }
        return first.model_copy(
            update={
                "observations": kept[:3],
                "stages": first.stages.model_copy(update=stages),
            }
        )

    return merge


def check_coaching(call: CallText) -> Callable[[Coaching], None]:
    """The rules that refuse the whole answer (module docstring), for
    call_model: the tone list and the summary language."""

    def check(answer: Coaching) -> None:
        written = _written(answer)
        errors: Errors = [
            (where, "harsh_tone") for where, text in written if harsh(text)
        ]
        if not in_language(" ".join(text for _, text in written), call.language):
            errors.append(("coaching", "wrong_language"))
        if errors:
            raise output_rejected(COACHING_LABEL, tuple(errors))

    return check


def agent_dialect(spoken: Spoken) -> str:
    """The agent's Arabic dialect as the roles pass heard it; MSA when it did
    not hear the agent, or heard a language that is not Arabic."""
    return spoken.agent if spoken.agent in ARABIC_LANGUAGES else MSA


def coaching_data(call: CallText, next_step: NextStepSeen = NO_NEXT_STEP) -> str:
    """The call's data, then the agent's dialect and stage 1's next step: booked
    and its kind (none for none, or a kind that is not one)."""
    kind = next_step.kind if next_step.kind in _NEXT_KINDS else None
    return (
        f"{call.data()}\n\n"
        f"AGENT DIALECT: {agent_dialect(call.spoken)}\n"
        f"NEXT STEP BOOKED: {'true' if next_step.booked else 'false'}\n"
        f"NEXT STEP KIND: {kind or 'none'}"
    )


async def coach(
    client: LLMClient,
    call: CallText,
    *,
    scope: TenantScope,
    settings: Settings,
    next_step: NextStepSeen = NO_NEXT_STEP,
) -> tuple[Coaching, LLMResponse]:
    """unit_b.coaching: one call, or two when the first answer is malformed
    or keeps no verified strength or improvement (merged_coaching, D-101).
    MalformedOutputError when still none. `next_step` is stage 1's, as
    coaching_data writes it. The answer comes back whole: what does not
    stand is left out of the part (coaching_part), so a re-run reads the
    kept answer the schema accepted."""
    answer, response = await call_model(
        client,
        build_call_prompt(COACHING_TEMPLATE, coaching_data(call, next_step)),
        Coaching,
        COACHING_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_B_COACHING,
        max_output_tokens=task_ceiling(
            settings,
            PROFILE_UNIT_B_COACHING,
            plain=COACHING_MAX_OUTPUT_TOKENS,
            reasoning=COACHING_REASONING_MAX_OUTPUT_TOKENS,
            client=client,
        ),
        check=check_coaching(call),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=REPROMPT_TAILS,
        mend=Mend(lambda found: coaching_failures(call, found), merged_coaching(call)),
    )
    failing = coaching_failures(call, answer)
    if failing:
        output_rejected(COACHING_LABEL, tuple(failing))
        raise MalformedOutputError()
    return relocated(call, answer), response


def _moment_start(call: CallText, moment: Moment) -> float | None:
    """When the moment's segment starts; None for a segment that does not exist."""
    index = call.index_of(moment.segment)
    return None if index is None else call.segments[index].start_s


def _stage(call: CallText, name: str, stage: Stage) -> dict[str, object]:
    """A stage as delivered: a yes whose quote failed kept yes, unverified,
    its quote and segment null; a failing quote given on a no dropped."""
    kept = stage.model_dump()
    failed = bool(_stage_errors(call, f"stages.{name}", stage))
    if failed:
        kept.update(quote=None, segment=None)
    return {**kept, "unverified": failed and stage.done == "yes"}


def coaching_evidence(call: CallText, answer: Coaching) -> tuple[int, int]:
    """(dropped, unverified): the observations and moments left out of the
    part, and the stages done yes kept unverified."""
    dropped = len(answer.observations) - len(_standing(call, answer))
    dropped += sum(
        1 for moment in answer.moments if _moment_start(call, moment) is None
    )
    unverified = sum(
        1
        for name in STAGE_NAMES
        if _stage(call, name, getattr(answer.stages, name))["unverified"]
    )
    return dropped, unverified


def coaching_part(
    call: CallText, answer: Coaching, loss_reason: str | None = None
) -> dict[str, object]:
    """Stage 2's coaching part: the observations that stand, the moments on
    real segments each timed from its segment in code, the plan, each stage
    as _stage delivers it, the language it is written in, and ask_why -- the
    fixed tip to ask the client why -- when stage 1's `loss_reason` is
    no_reason_given, else null."""
    moments = []
    for moment in answer.moments:
        start = _moment_start(call, moment)
        if start is not None:
            moments.append(
                {"timestamp": clock(start), "start_s": start, **moment.model_dump()}
            )
    return {
        "language": call.language,
        "observations": [seen.model_dump() for seen in _standing(call, answer)],
        "moments": moments,
        "plan": list(answer.plan),
        "stages": {
            name: _stage(call, name, getattr(answer.stages, name))
            for name in STAGE_NAMES
        },
        "ask_why": ASK_WHY[call.language] if loss_reason == NO_REASON_GIVEN else None,
    }
