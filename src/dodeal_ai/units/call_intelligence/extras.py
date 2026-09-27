"""unit_b.extras (wave 2): what the CRM files a call under, a message the agent
could send next, and how serious the client is -- the last read in code from
stage 1's analysis, never asked of the model (D-105).

  keywords     the projects, communities, developers and topics said: each as
               said, a name of at most five words, never a sentence, its
               words checked in the segment it cites, an English
               form where one exists, else null, and the tenant's canonical
               name when it is one on its keyword_vocabulary (config.py),
               else null and kept as found
  tags         outcome (moved_forward, stalled, needs_follow_up, dead), stage
               (first_contact, follow_up, viewing, negotiation, closing) and
               client_type (end_user, investor, broker, unknown)
  dialect      the agent's Arabic dialect (gulf_ar, egyptian_ar, levantine_ar,
               iraqi_ar, maghrebi_ar, msa_ar or unknown), a known one quoted
               from an AGENT segment
  whatsapp     a follow-up the agent may send, at most 60 words, in the
               CLIENT's language as the roles pass heard it (language.py),
               else the summary language: to an Arabic speaker in the agent's
               dialect when known, else the tenant's whatsapp_default_dialect.
               A SUGGESTION ONLY: this service sends nothing to anyone but the
               tenant's callback (core/callbacks.py); the text goes back to
               the CRM inside call.stage2 and nowhere else.
  seriousness  five yes-or-no checks, each decided in code (D-105): three
               from stage 1's details -- budget_stated, timeline_stated,
               decision_maker_named: yes when the detail was said (stated or
               uncertain), counted only when stated on its own verified
               quote; next_step_agreed from stage 1's next step, yes when
               there is one, counted only on its verified quote; and
               client_engaged from the talk itself, the score's engaged rule
               (score.engaged, the tenant's engaged_share and engaged_turns).
               The band is code's: A for 4 or 5 counted yes, B for 2 or 3, C
               for 0 or 1. Marked manager_only: for the agent's manager,
               never the agent. Null, with seriousness_reason
               analysis_unavailable, when stage 1 kept no analysis.

ONE JUDGE PER FACT (D-105). extras_v8 asked the model the same five
questions the extraction had already answered with its own quotes, so two
passes judged one fact, and a quote failing in either moved the band: one
run of a real call went B, C, B on a single next_step_agreed. The checks are
now read from the one judgement already made; extras_v9 asks nothing about
the client, and an answer kept under v8 still reads back, its seriousness
ignored.

THE CHECKS, in code, FIELD BY FIELD as the extraction's (passes.py):

  - evidence: every keyword's words in the segment it cites, a known agent
    dialect quoted from a segment of the agent's, and every quote given
    checked. A keyword whose quote fails is dropped; the agent dialect whose
    quote fails is kept with unverified true and its quote and segment null.
    Every failure is logged as quote_miss with why (D-105).
  - format: a keyword whose said is longer than MAX_KEYWORD_WORDS is dropped,
    and a canonical name not on the list becomes null. A WhatsApp text over
    60 words or not in its language's script (evidence.written_in) is
    reprompted once with a fixed tail (WHATSAPP_TAIL_TEMPLATE); failing again
    it is delivered null with whatsapp_reason (too_long, wrong_language).
  - the tags are always kept.

The pass fails -- one reprompt, then the extras part is null -- only on a
schema error or when more than half of its quotes fail. The language and
dialect the text was asked in (whatsapp_suggestion.language,
whatsapp_dialect) are code's, from the client's language, the checked agent
dialect and the default.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_EXTRAS, task_ceiling
from dodeal_ai.core.prompting import AssembledPrompt
from dodeal_ai.units.call_intelligence.alarms import words
from dodeal_ai.units.call_intelligence.evidence import (
    CallText,
    Cited,
    Errors,
    Quote,
    Said,
    SegmentId,
    Strict,
    evidence_errors,
    failed_quotes,
    log_misses,
    mostly_failed,
    quote_at,
    quote_errors,
    relocated,
    written_in,
)
from dodeal_ai.units.call_intelligence.language import message_language
from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    EXTRAS_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    WHATSAPP_TAIL_TEMPLATE,
    build_call_prompt,
    one_line,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

EXTRAS_LABEL = "llm.unit_b.extras"

# What the answer may cost (register item 15): fifteen keywords and a 60-word
# message, sized against an Arabic call on a non-reasoning model (it held five
# quoted checks too until extras_v9).
EXTRAS_MAX_OUTPUT_TOKENS = 2000
# The same answer on a reasoning profile, whose hidden reasoning is spent
# inside the ceiling (register item 116): the ceiling follows the profile.
EXTRAS_REASONING_MAX_OUTPUT_TOKENS = 4000

# More keywords than any honest call names.
MAX_KEYWORDS = 15

# The most words a keyword's `said` may carry: a name, never a sentence. Five
# holds a long project name; more lets a clause pass as a keyword, fewer
# refuses a true name and costs the pass a reprompt.
MAX_KEYWORD_WORDS = 5

# The longest WhatsApp suggestion, in words (BRD): a message, not a letter.
WHATSAPP_MAX_WORDS = 60

YES = "yes"
NO = "no"

# The reprompt tail by failure code (prompts.REPROMPT_TAILS), a WhatsApp
# text's own first: its tail restates the length and the script.
EXTRAS_TAILS: Mapping[str, str] = MappingProxyType(
    {
        "too_long": WHATSAPP_TAIL_TEMPLATE,
        "wrong_language": WHATSAPP_TAIL_TEMPLATE,
        **REPROMPT_TAILS,
    }
)

# The dialects the agent may be heard in (language.CALL_LANGUAGES' Arabic
# codes), and the four a tenant may write its messages in.
type AgentDialectName = Literal[
    "gulf_ar",
    "egyptian_ar",
    "levantine_ar",
    "iraqi_ar",
    "maghrebi_ar",
    "msa_ar",
    "unknown",
]
type WhatsAppDialect = Literal["gulf_ar", "egyptian_ar", "levantine_ar", "iraqi_ar"]
UNKNOWN_DIALECT = "unknown"
ARABIC = "ar"
# The tenant default when unit_b sets none: the Gulf, where the agencies are.
DEFAULT_WHATSAPP_DIALECT: WhatsAppDialect = "gulf_ar"

# The seriousness checks, and the bands from the top: the first whose floor
# the count of verified yes answers reaches.
SERIOUSNESS_CHECKS = (
    "budget_stated",
    "timeline_stated",
    "decision_maker_named",
    "next_step_agreed",
    "client_engaged",
)
SERIOUSNESS_BANDS = ((4, "A"), (2, "B"), (0, "C"))
# Why the seriousness is null: stage 1 kept no analysis to read it from.
ANALYSIS_UNAVAILABLE = "analysis_unavailable"
# Where a check was decided (D-105): stage 1's analysis, or code on the talk.
FROM_ANALYSIS = "analysis"
FROM_CODE = "code"
# The stage-1 detail each detail check reads, and a detail's states.
_DETAIL_OF = {
    "budget_stated": "budget",
    "timeline_stated": "timeline",
    "decision_maker_named": "decision_maker",
}
_STATED = "stated"
_UNCERTAIN = "uncertain"

# Lengths past any honest answer: a name, a reason, a message.
_NAME_CHARS = 120
_REASON_CHARS = 200
_WHATSAPP_CHARS = 600

type _Name = Annotated[str, Field(min_length=1, max_length=_NAME_CHARS)]
type _Reason = Annotated[str, Field(min_length=1, max_length=_REASON_CHARS)]


class Keyword(Strict):
    """A project, community, developer or topic, as said and in English."""

    kind: Literal["project", "community", "developer", "topic"]
    said: Said
    english: _Name | None
    # The tenant's listed name this keyword is; None when it is none. A default,
    # so an answer kept before the vocabulary existed still reads back.
    canonical: _Name | None = None
    segment: Cited


class Tags(Strict):
    outcome: Literal["moved_forward", "stalled", "needs_follow_up", "dead"]
    stage: Literal["first_contact", "follow_up", "viewing", "negotiation", "closing"]
    client_type: Literal["end_user", "investor", "broker", "unknown"]


class SeriousCheck(Strict):
    """One seriousness check: the answer, why, and a yes quoted."""

    answer: Literal["yes", "no"]
    reason: _Reason
    quote: Quote
    segment: SegmentId


class Seriousness(Strict):
    """extras_v8's five checks: read back from an answer kept under v8, and
    never used (D-105)."""

    budget_stated: SeriousCheck
    timeline_stated: SeriousCheck
    decision_maker_named: SeriousCheck
    next_step_agreed: SeriousCheck
    client_engaged: SeriousCheck


class AgentDialect(Strict):
    """The agent's dialect and the agent's words that show it; unknown with
    none."""

    dialect: AgentDialectName
    quote: Quote
    segment: SegmentId


# An answer kept before the dialect existed reads back as unknown.
_NO_DIALECT = AgentDialect(dialect=UNKNOWN_DIALECT, quote=None, segment=None)


class Extras(Strict):
    """unit_b.extras' answer, exactly."""

    keywords: Annotated[list[Keyword], Field(max_length=MAX_KEYWORDS)]
    tags: Tags
    agent_dialect: AgentDialect = _NO_DIALECT
    whatsapp: Annotated[str, Field(min_length=1, max_length=_WHATSAPP_CHARS)]
    # An answer kept under extras_v8 still reads back (paid.py re-runs pay for
    # nothing received); its seriousness is ignored. extras_v9 asks none.
    seriousness: Seriousness | None = None


@dataclass(frozen=True, slots=True)
class Heard:
    """One fact as stage 1's analysis holds it: whether it was said, whether
    it stands on its own verified quote, and that quote."""

    said: bool
    verified: bool
    quote: str | None = None
    segment: str | None = None


NOT_HEARD = Heard(said=False, verified=False)


@dataclass(frozen=True, slots=True)
class Facts:
    """What seriousness reads of stage 1's analysis (D-105), by check."""

    budget_stated: Heard = NOT_HEARD
    timeline_stated: Heard = NOT_HEARD
    decision_maker_named: Heard = NOT_HEARD
    next_step_agreed: Heard = NOT_HEARD


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _detail(held: object) -> Heard:
    """A stored detail: said when stated or uncertain; verified only when
    stated, since a detail whose quote failed is kept uncertain (passes.py)."""
    if not isinstance(held, dict) or held.get("state") not in (_STATED, _UNCERTAIN):
        return NOT_HEARD
    if held.get("state") != _STATED or held.get("evidence_failed") is not False:
        return Heard(said=True, verified=False)
    return Heard(True, True, _text(held.get("quote")), _text(held.get("segment")))


def _step(held: object) -> Heard:
    """The stored next step: said when it has an action; verified when its
    quote held (unverified false)."""
    if not isinstance(held, dict) or held.get("action") is None:
        return NOT_HEARD
    if held.get("unverified") is not False:
        return Heard(said=True, verified=False)
    return Heard(True, True, _text(held.get("quote")), _text(held.get("segment")))


def facts_of(analysis: object) -> Facts | None:
    """What seriousness reads of stage 1's stored analysis (D-105); None when
    the analysis is null, so there is nothing to read."""
    if not isinstance(analysis, dict):
        return None
    details = analysis.get("details")
    elements = analysis.get("elements")
    held = details if isinstance(details, dict) else {}
    step = elements.get("next_step") if isinstance(elements, dict) else None
    return Facts(
        **{name: _detail(held.get(detail)) for name, detail in _DETAIL_OF.items()},
        next_step_agreed=_step(step),
    )


def seriousness_band(yes: int) -> str:
    """The band a count of yes answers falls in."""
    return next(band for floor, band in SERIOUSNESS_BANDS if yes >= floor)


def whatsapp_language(call: CallText) -> str:
    """The language the suggestion is written in: the client's."""
    return message_language(call.spoken.client, call.language)


def whatsapp_dialect(
    call: CallText, agent: AgentDialect, default: WhatsAppDialect
) -> str | None:
    """The dialect the suggestion was asked in: none unless it is in Arabic;
    then the agent's when known, else the tenant's default."""
    if whatsapp_language(call) != ARABIC:
        return None
    return default if agent.dialect == UNKNOWN_DIALECT else agent.dialect


def _dialect_errors(call: CallText, agent: AgentDialect) -> Errors:
    """A known dialect is owed a quote; any quote is the agent's own."""
    owed = quote_errors if agent.dialect == UNKNOWN_DIALECT else evidence_errors
    return owed(call, "agent_dialect", agent.quote, agent.segment, speaker=AGENT)


def _whatsapp_errors(call: CallText, text: str) -> Errors:
    errors: Errors = []
    if len(text.split()) > WHATSAPP_MAX_WORDS:
        errors.append(("whatsapp", "too_long"))
    if not written_in(text, whatsapp_language(call)):
        errors.append(("whatsapp", "wrong_language"))
    return errors


def extras_data(
    call: CallText,
    vocabulary: Iterable[str],
    default_dialect: WhatsAppDialect = DEFAULT_WHATSAPP_DIALECT,
) -> str:
    """The call's data, then the tenant's vocabulary, one name per line, then
    the client's language to write in and the default dialect."""
    listed = [f"- {one_line(term)}" for term in sorted(vocabulary)]
    shown = "\n".join(["VOCABULARY:", *listed]) if listed else "VOCABULARY: none"
    return (
        f"{call.data()}\n\n{shown}\n\n"
        f"WHATSAPP LANGUAGE: {whatsapp_language(call)}\n"
        f"DEFAULT DIALECT: {default_dialect}"
    )


def extras_quotes(call: CallText, answer: Extras) -> dict[str, Errors]:
    """Every quote the extras give or owe, by where it is, with the quote
    check's failures ([] for one that passes)."""
    found: dict[str, Errors] = {}
    for n, keyword in enumerate(answer.keywords):
        where = f"keywords.{n}"
        found[where] = evidence_errors(call, where, keyword.said, keyword.segment)
    agent = answer.agent_dialect
    if agent.dialect != UNKNOWN_DIALECT or (agent.quote, agent.segment) != (None, None):
        found["agent_dialect"] = _dialect_errors(call, agent)
    return found


def extras_evidence(call: CallText, answer: Extras) -> tuple[int, int]:
    """(dropped, unverified): the keywords whose quotes failed, dropped, and
    the agent dialect kept unverified."""
    failing = [where for where, errors in extras_quotes(call, answer).items() if errors]
    dropped = sum(1 for where in failing if where.startswith("keywords."))
    return dropped, len(failing) - dropped


class Attempts:
    """The pass's client, counting the answers received, so the check knows
    when the one reprompt is spent (a WhatsApp text is then kept null)."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self.received = 0

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
            response_schema=response_schema,
        )
        self.received += 1
        return response


def check_extras(
    call: CallText, attempts: Attempts | None = None
) -> Callable[[Extras], None]:
    """The rules the schema cannot hold (module docstring), for call_model:
    malformed when more than half of the quotes fail, and -- while the
    reprompt is not spent -- on a WhatsApp text too long or in the wrong
    script, so the reprompt carries the WhatsApp tail. Every quote that
    fails is logged as quote_miss with why (D-105)."""

    def check(answer: Extras) -> None:
        quotes = extras_quotes(call, answer)
        log_misses(EXTRAS_LABEL, call, failed_quotes(quotes), quote_at(answer))
        spent = attempts is not None and attempts.received > 1
        message = [] if spent else _whatsapp_errors(call, answer.whatsapp)
        if message or mostly_failed(quotes):
            errors = message + failed_quotes(quotes)
            raise output_rejected(EXTRAS_LABEL, tuple(errors))

    return check


async def find_extras(
    client: LLMClient,
    call: CallText,
    *,
    scope: TenantScope,
    settings: Settings,
    vocabulary: frozenset[str] = frozenset(),
    default_dialect: WhatsAppDialect = DEFAULT_WHATSAPP_DIALECT,
) -> tuple[Extras, LLMResponse]:
    """unit_b.extras: one call, or two when the first answer is malformed;
    `vocabulary` is the tenant's keyword_vocabulary, `default_dialect` its
    whatsapp_default_dialect."""
    attempts = Attempts(client)
    answer, response = await call_model(
        attempts,
        build_call_prompt(
            EXTRAS_TEMPLATE, extras_data(call, vocabulary, default_dialect)
        ),
        Extras,
        EXTRAS_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_B_EXTRAS,
        max_output_tokens=task_ceiling(
            settings,
            PROFILE_UNIT_B_EXTRAS,
            plain=EXTRAS_MAX_OUTPUT_TOKENS,
            reasoning=EXTRAS_REASONING_MAX_OUTPUT_TOKENS,
            client=client,
        ),
        check=check_extras(call, attempts),
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        tail_by_error=EXTRAS_TAILS,
    )
    return relocated(call, answer), response


def _kept(found: AgentDialect, failed: bool) -> dict[str, object]:
    """A quoted answer as delivered: kept with unverified true and no quote
    when its quote failed."""
    kept = found.model_dump()
    if failed:
        kept.update(quote=None, segment=None)
    return {**kept, "unverified": failed}


def _keyword(keyword: Keyword, vocabulary: frozenset[str]) -> dict[str, object]:
    """A kept keyword as delivered: a canonical name off the list is null."""
    kept = keyword.model_dump()
    if keyword.canonical not in vocabulary:
        kept["canonical"] = None
    return kept


def _checked(heard: Heard, source: str) -> dict[str, object]:
    """One seriousness check as delivered: yes when said, its quote when it
    stands, unverified when said but not on a verified quote."""
    return {
        "answer": YES if heard.said else NO,
        "source": source,
        "quote": heard.quote,
        "segment": heard.segment,
        "unverified": heard.said and not heard.verified,
    }


def seriousness_part(facts: Facts | None, engaged: bool) -> dict[str, object] | None:
    """The seriousness (D-105): each check from stage 1's analysis, the
    client's engagement from the talk, and the band from the verified yes
    answers; None when there is no analysis to read."""
    if facts is None:
        return None
    checks = {
        name: _checked(getattr(facts, name), FROM_ANALYSIS)
        for name in SERIOUSNESS_CHECKS
        if name != "client_engaged"
    }
    checks["client_engaged"] = _checked(Heard(engaged, engaged), FROM_CODE)
    yes = sum(
        1
        for check in checks.values()
        if check["answer"] == YES and check["unverified"] is False
    )
    return {
        "band": seriousness_band(yes),
        "yes": yes,
        "manager_only": True,
        "checks": {name: checks[name] for name in SERIOUSNESS_CHECKS},
    }


def extras_part(
    call: CallText,
    answer: Extras,
    default_dialect: WhatsAppDialect = DEFAULT_WHATSAPP_DIALECT,
    vocabulary: frozenset[str] = frozenset(),
    *,
    facts: Facts | None = None,
    engaged: bool = False,
) -> dict[str, object]:
    """Stage 2's extras part: the keywords whose quotes hold, the tags, the
    agent's dialect, the WhatsApp suggestion in the language and dialect code
    says it was asked in, and the seriousness code reads from stage 1's
    `facts` and whether the client `engaged` (seriousness_part), with
    seriousness_reason when it is null."""
    failed = {where for where, errors in extras_quotes(call, answer).items() if errors}
    refused = _whatsapp_errors(call, answer.whatsapp)
    serious = seriousness_part(facts, engaged)
    return {
        "keywords": [
            _keyword(keyword, vocabulary)
            for n, keyword in enumerate(answer.keywords)
            if f"keywords.{n}" not in failed
            and len(words(keyword.said)) <= MAX_KEYWORD_WORDS
        ],
        "tags": answer.tags.model_dump(),
        "agent_dialect": _kept(answer.agent_dialect, "agent_dialect" in failed),
        "whatsapp_dialect": whatsapp_dialect(
            call, answer.agent_dialect, default_dialect
        ),
        "whatsapp_suggestion": (
            None
            if refused
            else {"language": whatsapp_language(call), "text": answer.whatsapp}
        ),
        "whatsapp_reason": refused[0][1] if refused else None,
        "seriousness": serious,
        "seriousness_reason": ANALYSIS_UNAVAILABLE if serious is None else None,
    }
