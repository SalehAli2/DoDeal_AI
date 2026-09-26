"""Unit B's prompt set: the versioned templates, their stamp, and the one way a
transcript is written into a prompt.

THE TEMPLATES are files under prompts/call_intelligence/, like Unit A's: the
extraction and prose passes (wave 1), each wave 2 pass, and the reprompt tail
every pass shares.
PROMPT_SET_VERSION names the texts; a test pins their digest beside it
(tests/unit/test_unit_b_prompt_set_stamp.py), so an edit without a bump fails.
A template is never edited once stamped: a change is a new file, and the one
it replaces stays in the set, unsent, so the digest covers every shipped file.

THE TRANSCRIPT COMES FIRST, inside the delimited data half, and the
instructions after it (AssembledPrompt.data_first). One line per segment:

    [s<n> mm:ss agent|client] text

`s<n>` is the segment's id, 1-based in transcript order: the id a quote
cites and the check in code looks up. Whitespace inside a segment is folded
to single spaces, so no segment can start a line of its own and pass for
another segment or for the LANGUAGE line. The delimiters are neutralised by
build_prompt, as for a note.

WHO SPOKE: a segment whose speaker label is "agent" is the agent; every other
label is the client. A provisional rule until voice ID exists: diarisation
labels are the transcriber's, and only the agent is named by it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from dodeal_ai.core.prompting import AssembledPrompt, build_prompt
from dodeal_ai.units.call_intelligence.transcriber import Segment

ROLES_TEMPLATE = "call_intelligence/roles_v4.txt"
EXTRACT_TEMPLATE = "call_intelligence/extract_v6.txt"
PROSE_TEMPLATE = "call_intelligence/prose_v2.txt"
OBJECTIONS_TEMPLATE = "call_intelligence/objections_v2.txt"
SCORE_TEMPLATE = "call_intelligence/score_v2.txt"
ESCALATIONS_TEMPLATE = "call_intelligence/escalations_v3.txt"
COACHING_TEMPLATE = "call_intelligence/coaching_v3.txt"
EXTRAS_TEMPLATE = "call_intelligence/extras_v8.txt"
TRANSLATE_TEMPLATE = "call_intelligence/translate_v2.txt"
WHATSAPP_TEMPLATE = "call_intelligence/whatsapp_v3.txt"
REPROMPT_TAIL_TEMPLATE = "call_intelligence/reprompt_tail_v1.txt"
QUOTE_LENGTH_TAIL_TEMPLATE = "call_intelligence/reprompt_tail_quote_length_v1.txt"
QUOTE_EXACT_TAIL_TEMPLATE = "call_intelligence/reprompt_tail_quote_exact_v1.txt"
# The extras pass's tail for a WhatsApp text too long or in the wrong script
# (extras.EXTRAS_TAILS), unit_b_prompts_v13.
WHATSAPP_TAIL_TEMPLATE = "call_intelligence/reprompt_tail_whatsapp_v1.txt"

# The reprompt tail by failure code, in priority order: the first code here
# that any failure carries picks it, else REPROMPT_TAIL_TEMPLATE. Fixed files
# only: the rejected answer is never sent.
REPROMPT_TAILS: Mapping[str, str] = MappingProxyType(
    {
        "quote_length": QUOTE_LENGTH_TAIL_TEMPLATE,
        "quote_not_in_segment": QUOTE_EXACT_TAIL_TEMPLATE,
    }
)

# Replaced by extract_v2 (evidence for every element) and extras_v2 (the
# keyword vocabulary), then every quoting template by its successor with the
# 15-word, one-segment quote rule (unit_b_prompts_v6), then extras_v3 by
# extras_v4 (the agent's dialect, unit_b_prompts_v8), then roles_v2 by roles_v3
# (each side's language, unit_b_prompts_v9), then extras_v4 by extras_v5 (the
# message in the client's language, unit_b_prompts_v10), then extract_v3,
# extras_v5 and escalations_v2 by extract_v4 (the next step's kind, time and
# booking, the two property details, the loss reason), extras_v6 (a keyword
# is a name of at most five words) and escalations_v3 (possible_broker),
# unit_b_prompts_v12, then every template that writes Arabic by one naming
# the roles in Arabic (unit_b_prompts_v14), then extras_v7 and whatsapp_v2 by
# the two with example phrasings per Arabic dialect (unit_b_prompts_v15),
# then roles_v3 by roles_v4 (each voice's languages from the whole call) and
# extract_v5 by extract_v6 (quoted topics, short relative times, a clear
# agreement booking), unit_b_prompts_v16; never sent again.
RETIRED_TEMPLATES: tuple[str, ...] = (
    "call_intelligence/extract_v1.txt",
    "call_intelligence/extras_v1.txt",
    "call_intelligence/roles_v1.txt",
    "call_intelligence/extract_v2.txt",
    "call_intelligence/objections_v1.txt",
    "call_intelligence/score_v1.txt",
    "call_intelligence/escalations_v1.txt",
    "call_intelligence/coaching_v1.txt",
    "call_intelligence/extras_v2.txt",
    "call_intelligence/extras_v3.txt",
    "call_intelligence/roles_v2.txt",
    "call_intelligence/extras_v4.txt",
    "call_intelligence/extract_v3.txt",
    "call_intelligence/extras_v5.txt",
    "call_intelligence/escalations_v2.txt",
    "call_intelligence/extract_v4.txt",
    "call_intelligence/prose_v1.txt",
    "call_intelligence/coaching_v2.txt",
    "call_intelligence/extras_v6.txt",
    "call_intelligence/translate_v1.txt",
    "call_intelligence/whatsapp_v1.txt",
    "call_intelligence/extras_v7.txt",
    "call_intelligence/whatsapp_v2.txt",
    "call_intelligence/roles_v3.txt",
    "call_intelligence/extract_v5.txt",
)

# The stamp stage 1 carries under versions.prompt. Move it with the digest
# in the stamp test whenever one of UNIT_B_TEMPLATES changes.
PROMPT_SET_VERSION = "unit_b_prompts_v16"

# Every template Unit B can send, in pass order, then the retired ones; the
# worker preloads them all.
UNIT_B_TEMPLATES: tuple[str, ...] = (
    ROLES_TEMPLATE,
    EXTRACT_TEMPLATE,
    PROSE_TEMPLATE,
    OBJECTIONS_TEMPLATE,
    SCORE_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    COACHING_TEMPLATE,
    EXTRAS_TEMPLATE,
    TRANSLATE_TEMPLATE,
    WHATSAPP_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    QUOTE_LENGTH_TAIL_TEMPLATE,
    QUOTE_EXACT_TAIL_TEMPLATE,
    WHATSAPP_TAIL_TEMPLATE,
    *RETIRED_TEMPLATES,
)

# The templates whose answer may be written in Arabic: each names the agent
# "الوكيل" (or "مندوب المبيعات") and the client "العميل", never the reverse;
# and the one that reads Arabic but writes none (the roles pass answers codes).
ARABIC_WRITING_TEMPLATES: tuple[str, ...] = (
    EXTRACT_TEMPLATE,
    PROSE_TEMPLATE,
    COACHING_TEMPLATE,
    EXTRAS_TEMPLATE,
    TRANSLATE_TEMPLATE,
    WHATSAPP_TEMPLATE,
)
ARABIC_READING_ONLY: tuple[str, ...] = (ROLES_TEMPLATE,)

# The templates that write the WhatsApp message: each shows example phrasings
# of the Egyptian, Gulf and Levantine dialects, so a message to an Arabic
# speaker is written in the dialect and not in formal Arabic.
WHATSAPP_WRITING_TEMPLATES: tuple[str, ...] = (EXTRAS_TEMPLATE, WHATSAPP_TEMPLATE)

# What the API process sends itself (the WhatsApp route, whatsapp.py); the
# lifespan preloads them beside Unit A's.
API_TEMPLATES: tuple[str, ...] = (WHATSAPP_TEMPLATE, REPROMPT_TAIL_TEMPLATE)

AGENT = "agent"
CLIENT = "client"
# A voice no role mapping named: an engine's own label (speaker_N) left as it
# was because the roles were not applied, or an engine that names no speaker.
UNKNOWN = "unknown"
ENGINE_LABEL = re.compile(r"^speaker_[0-9]{1,3}$")

_WHITESPACE = re.compile(r"\s+")


def segment_id(index: int) -> str:
    """The id of the segment at `index` (0-based): s1, s2, ..."""
    return f"s{index + 1}"


def role_of(segment: Segment) -> str:
    """agent for the agent's label, client for every other."""
    return AGENT if segment.speaker == AGENT else CLIENT


def said_by(segment: Segment) -> str:
    """Who a number or an alarm phrase is put down to: unknown while the
    segment's label is one no role mapping named, else its role_of."""
    if segment.speaker == UNKNOWN or ENGINE_LABEL.match(segment.speaker):
        return UNKNOWN
    return role_of(segment)


def clock(seconds: float) -> str:
    """mm:ss from the start of the call; minutes run past 59."""
    whole = int(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def one_line(text: str) -> str:
    """Every run of whitespace, newlines included, as one space."""
    return _WHITESPACE.sub(" ", text).strip()


def render_transcript(
    segments: Sequence[Segment], texts: Sequence[str] | None = None
) -> str:
    """The transcript as the prompt shows it. `texts`, one per segment, is
    what to show instead of each segment's own text (the masked copy)."""
    shown = [segment.text for segment in segments] if texts is None else texts
    return "\n".join(
        f"[{segment_id(n)} {clock(segment.start_s)} {role_of(segment)}] "
        f"{one_line(text)}"
        for n, (segment, text) in enumerate(zip(segments, shown, strict=True))
    )


def build_call_prompt(template: str, caller_data: str) -> AssembledPrompt:
    """`template` after the delimited `caller_data`, which starts with the
    transcript."""
    return build_prompt(template, caller_data=caller_data, data_first=True)
