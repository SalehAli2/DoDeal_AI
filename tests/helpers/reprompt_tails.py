"""The exact reprompt tail a Unit B pass sends (D-84): its fixed file's text,
then the failed-fields block, one `$.path: code` line per failure. Written out
here by hand, never rendered by core/prompting.py, so a test compares the
code's block against an independent copy."""

from __future__ import annotations

from dodeal_ai.core.prompting import build_prompt

FAILED_START = "----- FAILED FIELDS (path: code) -----"
FAILED_END = "----- END FAILED FIELDS -----"


def tail_with(template: str, *lines: str) -> str:
    """`template`'s stripped text, a blank line, and the block of `lines`."""
    fixed = build_prompt(template, caller_data="").stable
    return "\n".join((f"{fixed}\n", FAILED_START, *lines, FAILED_END))
