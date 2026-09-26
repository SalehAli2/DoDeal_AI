"""unit_b.extras (wave 2): what the CRM files a call under, a message the agent
could send next, and how serious the client is.

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
  seriousness  five yes-or-no checks -- budget_stated, timeline_stated,
               decision_maker_named, next_step_agreed, client_engaged -- each
               with a short reason and every yes quoted. The band is code's:
               A for 4 or 5 yes, B for 2 or 3, C for 0 or 1. Marked
               manager_only: for the agent's manager, never the agent.

THE CHECKS, in code, FIELD BY FIELD as the extraction's (passes.py):

  - evidence: every keyword's words in the segment it cites, every yes
    quoted and every quote given checked, a known agent dialect quoted from a
    segment of the agent's. A keyword whose quote fails is dropped; a
    seriousness check or the agent dialect whose quote fails is kept with
    unverified true and its quote and segment null, and an unverified yes
    does not count toward the band.
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
    mostly_failed,
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

# What the answer may cost (register item 15): fifteen keywords, a 60-word
# message and five quoted checks, sized against an Arabic call on a
# non-reasoning model.
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
# the count of yes answers reaches.
SERIOUSNESS_CHECKS = (
    "budget_stated",
    "timeline_stated",
    "decision_maker_named",
    "next_step_agreed",
    "client_engaged",
)
SERIOUSNESS_BANDS = ((4, "A"), (2, "B"), (0, "C"))

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
    seriousness: Seriousness


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
    for name in SERIOUSNESS_CHECKS:
        check: SeriousCheck = getattr(answer.seriousness, name)
        if check.answer == YES or (check.quote, check.segment) != (None, None):
            owed = evidence_errors if check.answer == YES else quote_errors
            where = f"seriousness.{name}"
            found[where] = owed(call, where, check.quote, check.segment)
    agent = answer.agent_dialect
    if agent.dialect != UNKNOWN_DIALECT or (agent.quote, agent.segment) != (None, None):
        found["agent_dialect"] = _dialect_errors(call, agent)
    return found


def extras_evidence(call: CallText, answer: Extras) -> tuple[int, int]:
    """(dropped, unverified): the keywords whose quotes failed, dropped, and
    the seriousness checks and agent dialect kept unverified."""
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
    script, so the reprompt carries the WhatsApp tail."""

    def check(answer: Extras) -> None:
        quotes = extras_quotes(call, answer)
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


def _kept(found: SeriousCheck | AgentDialect, failed: bool) -> dict[str, object]:
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


def extras_part(
    call: CallText,
    answer: Extras,
    default_dialect: WhatsAppDialect = DEFAULT_WHATSAPP_DIALECT,
    vocabulary: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Stage 2's extras part: the keywords whose quotes hold, the tags, the
    agent's dialect, the WhatsApp suggestion in the language and dialect code
    says it was asked in, and the seriousness checks with the band code gives
    from the verified yes answers (module docstring)."""
    failed = {where for where, errors in extras_quotes(call, answer).items() if errors}
    refused = _whatsapp_errors(call, answer.whatsapp)
    checks = {
        name: _kept(getattr(answer.seriousness, name), f"seriousness.{name}" in failed)
        for name in SERIOUSNESS_CHECKS
    }
    yes = sum(
        1
        for check in checks.values()
        if check["answer"] == YES and check["unverified"] is False
    )
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
        "seriousness": {
            "band": seriousness_band(yes),
            "yes": yes,
            "manager_only": True,
            "checks": checks,
        },
    }
