"""Unit B's prompt set and its stamp move together, or the build fails.

The same guard as Unit A's (register item 146): the three templates
UNIT_B_TEMPLATES names are hashed, name and stripped text, and the digest is
pinned beside PROMPT_SET_VERSION. A prompt edit without a bump is a red build.
Both constants are written down by hand, together, when the stamp moves.
"""

from __future__ import annotations

import hashlib

from dodeal_ai.core import prompting
from dodeal_ai.units.call_intelligence.prompts import (
    PROMPT_SET_VERSION,
    UNIT_B_TEMPLATES,
)

PROMPT_SET = "unit_b_prompts_v1"
PROMPT_SET_DIGEST = "61db949fa483baad224c9594b2d07f543b93fcb1b24d2dc6fc916e2e8b83b29f"

_HOW_TO_UPDATE = (
    "A Unit B prompt template changed. Bump PROMPT_SET_VERSION in "
    "units/call_intelligence/prompts.py and update PROMPT_SET and "
    "PROMPT_SET_DIGEST here, in the same commit."
)


def _digest() -> str:
    """sha256 over every template name and its stripped text, in tuple order,
    read from the shipped directory and never a DODEAL_PROMPTS_DIR."""
    digest = hashlib.sha256()
    for name in UNIT_B_TEMPLATES:
        text = (prompting._DEFAULT_PROMPTS_DIR / name).read_text(encoding="utf-8")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def test_the_unit_b_prompt_set_digest_matches_its_stamp() -> None:
    """The three shipped templates hash to the digest pinned for this stamp."""
    assert PROMPT_SET_VERSION == PROMPT_SET, _HOW_TO_UPDATE
    assert _digest() == PROMPT_SET_DIGEST, _HOW_TO_UPDATE
