"""unit_b.coaching (wave 2): coaching for the agent, in the summary language.

  observations  two or three, at least one strength and one improvement, each
                quoting the segment it rests on; every improvement with a
                "say it like this"
  moments       up to four: the segment, what happened, what would be better;
                its timestamp is the segment's start, read in code
  plan          a development plan of exactly three actions
  stages        opening, rapport, discovery, qualification, presentation,
                objections and close: each done yes or no, a yes quoted

THE CHECKS, all in code, any failure a malformed answer (one reprompt, then
the pass fails and the coaching part is null): every quote under the quote
check; a strength and an improvement both there; every moment's segment real;
the words written at least 60 % in the summary language's script, quotes left
out (evidence.in_language); and THE TONE CHECK (BRD P6) -- no phrase of
tone_list_v1, absolute or harsh, in English or Arabic, matched as the alarm
phrases are (alarms.py: folded, proclitics, two-word gaps).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_COACHING, task_ceiling
from dodeal_ai.units.call_intelligence.alarms import phrase_in, words
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Cited,
    Errors,
    Quote,
    Said,
    SegmentId,
    Strict,
    evidence_errors,
    in_language,
    quote_errors,
)
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    build_call_prompt,
    clock,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
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


class Observation(Strict):
    """A strength or an improvement, the quote it rests on, and for an
    improvement, the words to use instead."""

    kind: Literal["strength", "improvement"]
    text: _Text
    quote: Said
    segment: Cited
    say_it_like_this: Annotated[str | None, Field(max_length=_TEXT_CHARS)]


class Moment(Strict):
    """A moment on the call, and what would have been better."""

    segment: Cited
    what_happened: _Text
    better: _Text


class Stage(Strict):
    """One stage of the call: done or not, a yes quoted."""

    done: Literal["yes", "no"]
    quote: Quote
    segment: SegmentId


class Stages(Strict):
    opening: Stage
    rapport: Stage
    discovery: Stage
    qualification: Stage
    presentation: Stage
    objections: Stage
    close: Stage


class Coaching(Strict):
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


def _observation_errors(call: CallText, answer: Coaching) -> Errors:
    errors: Errors = []
    kinds = {seen.kind for seen in answer.observations}
    for kind in (STRENGTH, IMPROVEMENT):
        if kind not in kinds:
            errors.append(("observations", f"no_{kind}"))
    for n, seen in enumerate(answer.observations):
        where = f"observations.{n}"
        errors += evidence_errors(call, where, seen.quote, seen.segment)
        if seen.kind == IMPROVEMENT and seen.say_it_like_this is None:
            errors.append((where, "say_it_like_this_missing"))
    return errors


def check_coaching(call: CallText) -> Callable[[Coaching], None]:
    """The rules the schema cannot hold (module docstring), for call_model."""

    def check(answer: Coaching) -> None:
        errors = _observation_errors(call, answer)
        for n, moment in enumerate(answer.moments):
            if call.index_of(moment.segment) is None:
                errors.append((f"moments.{n}", "segment_unknown"))
        for name in STAGE_NAMES:
            stage: Stage = getattr(answer.stages, name)
            owed = evidence_errors if stage.done == "yes" else quote_errors
            errors += owed(call, f"stages.{name}", stage.quote, stage.segment)
        written = _written(answer)
        errors += [(where, "harsh_tone") for where, text in written if harsh(text)]
        if not in_language(" ".join(text for _, text in written), call.language):
            errors.append(("coaching", "wrong_language"))
        if errors:
            raise output_rejected(COACHING_LABEL, tuple(errors))

    return check


async def coach(
    client: LLMClient, call: CallText, *, scope: TenantScope, settings: Settings
) -> tuple[Coaching, LLMResponse]:
    """unit_b.coaching: one call, or two when the first answer is malformed."""
    return await call_model(
        client,
        build_call_prompt(COACHING_TEMPLATE, call.data()),
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
    )


def coaching_part(call: CallText, answer: Coaching) -> dict[str, object]:
    """Stage 2's coaching part: the answer, each moment timed from its
    segment in code, and the language it is written in."""
    moments = []
    for moment in answer.moments:
        index = call.index_of(moment.segment)
        assert index is not None  # the check found every moment's segment
        start = call.segments[index].start_s
        moments.append(
            {"timestamp": clock(start), "start_s": start, **moment.model_dump()}
        )
    return {
        "language": call.language,
        "observations": [seen.model_dump() for seen in answer.observations],
        "moments": moments,
        "plan": list(answer.plan),
        "stages": answer.stages.model_dump(),
    }
