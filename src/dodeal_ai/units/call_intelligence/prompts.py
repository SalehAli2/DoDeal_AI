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
from collections.abc import Sequence

from dodeal_ai.core.prompting import AssembledPrompt, build_prompt
from dodeal_ai.units.call_intelligence.transcriber import Segment

ROLES_TEMPLATE = "call_intelligence/roles_v1.txt"
EXTRACT_TEMPLATE = "call_intelligence/extract_v2.txt"
PROSE_TEMPLATE = "call_intelligence/prose_v1.txt"
OBJECTIONS_TEMPLATE = "call_intelligence/objections_v1.txt"
SCORE_TEMPLATE = "call_intelligence/score_v1.txt"
ESCALATIONS_TEMPLATE = "call_intelligence/escalations_v1.txt"
COACHING_TEMPLATE = "call_intelligence/coaching_v1.txt"
EXTRAS_TEMPLATE = "call_intelligence/extras_v2.txt"
REPROMPT_TAIL_TEMPLATE = "call_intelligence/reprompt_tail_v1.txt"

# Replaced by extract_v2 (evidence for every element) and extras_v2 (the
# keyword vocabulary); never sent again.
RETIRED_TEMPLATES: tuple[str, ...] = (
    "call_intelligence/extract_v1.txt",
    "call_intelligence/extras_v1.txt",
)

# The stamp stage 1 carries under versions.prompt. Move it with the digest
# in the stamp test whenever one of UNIT_B_TEMPLATES changes.
PROMPT_SET_VERSION = "unit_b_prompts_v4"

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
    REPROMPT_TAIL_TEMPLATE,
    *RETIRED_TEMPLATES,
)

AGENT = "agent"
CLIENT = "client"

_WHITESPACE = re.compile(r"\s+")


def segment_id(index: int) -> str:
    """The id of the segment at `index` (0-based): s1, s2, ..."""
    return f"s{index + 1}"


def role_of(segment: Segment) -> str:
    """agent for the agent's label, client for every other."""
    return AGENT if segment.speaker == AGENT else CLIENT


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
