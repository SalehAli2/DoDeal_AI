"""Unit B's prompt set and its stamp move together, or the build fails.

The same guard as Unit A's (register item 146): the templates UNIT_B_TEMPLATES
names are hashed, name and stripped text, and the digest is pinned beside
PROMPT_SET_VERSION. A prompt edit without a bump is a red build.
Both constants are written down by hand, together, when the stamp moves.

Each template is pinned on its own too. A shipped file is never edited -- a
change is a new file -- so a pin moves only when a file is added, and a pin
changed for an existing name is a stamped template edited.
"""

from __future__ import annotations

import hashlib

from dodeal_ai.core import prompting
from dodeal_ai.units.call_intelligence.prompts import (
    PROMPT_SET_VERSION,
    UNIT_B_TEMPLATES,
)

PROMPT_SET = "unit_b_prompts_v3"
PROMPT_SET_DIGEST = "f0ba9a0667fa835ca3166affa37bccff6e73f0101b4611adb259de2364ff155a"

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
    """The shipped templates hash to the digest pinned for this stamp."""
    assert PROMPT_SET_VERSION == PROMPT_SET, _HOW_TO_UPDATE
    assert _digest() == PROMPT_SET_DIGEST, _HOW_TO_UPDATE


# Each shipped template's sha256, over its stripped text.
TEMPLATE_DIGESTS = {
    "call_intelligence/extract_v2.txt": (
        "1d7fa2d4cd37248cbfe7f76b6f28932ae665a9ce23bf6154e77c85ebd1273190"
    ),
    "call_intelligence/prose_v1.txt": (
        "081f1bceb3b70232525416d39350f35bdd2963d59488c5f7a9dadf11df23efe8"
    ),
    "call_intelligence/objections_v1.txt": (
        "fc6bf0a517c76356f72676f5fbab4d95688b115939237b5156da3d61e7f1f3c0"
    ),
    "call_intelligence/score_v1.txt": (
        "1313ff49da49c99ad8a710dc2975954b4c4c15851a24c4521c49060bce0df403"
    ),
    "call_intelligence/escalations_v1.txt": (
        "3b993bfe33c0b6f8a87529dd61f3b28613a8cfa1c8e1222021ce577ed6029df7"
    ),
    "call_intelligence/coaching_v1.txt": (
        "2ce19501575ebd005a99c0e8957ceb395bc26eb665907a226c3618c7b0b76898"
    ),
    "call_intelligence/extras_v1.txt": (
        "85eee2b53e62d8d842947369a30df0e8dce8255789d7015a7ae5a14a2ae55e00"
    ),
    "call_intelligence/extras_v2.txt": (
        "a9db4f99aefb2dbc3420b24084b659b1c5fe2a01a5d1681a7ceb23f6d986c011"
    ),
    "call_intelligence/reprompt_tail_v1.txt": (
        "a342aec7b7a3ad284e2d64c636729b935bc6dc87e749408a5009befef3445a66"
    ),
    "call_intelligence/extract_v1.txt": (
        "7ccf78ffa14164c08f5f21b90b73864354559cf9f0d99e1a7e8880ea30ae839a"
    ),
}


def test_no_shipped_template_is_ever_edited() -> None:
    """Every template in the set is pinned, and hashes to its own pin."""
    shipped = {
        name: hashlib.sha256(
            (prompting._DEFAULT_PROMPTS_DIR / name)
            .read_text(encoding="utf-8")
            .strip()
            .encode("utf-8")
        ).hexdigest()
        for name in UNIT_B_TEMPLATES
    }
    assert shipped == TEMPLATE_DIGESTS
