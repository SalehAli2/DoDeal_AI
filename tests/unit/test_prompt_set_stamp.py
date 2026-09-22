"""The prompt set and its stamp move together, or the build fails.

Register item 146. `d1b4146` edited `score_v2.txt` and left
`PROMPT_SET_VERSION` at `unit_a_prompts_v2`, so two different prompt texts
shipped under one stamp and a judgement stamped v2 stopped being reproducible
from the v2 files. Nothing failed: the stamp is a string, and a string agrees
with whatever the files happen to say.

This is the guard. It hashes every template `UNIT_A_TEMPLATES` names and pins
the digest beside the stamp, so a prompt edit without a bump is a red build and
not a discovery six weeks later in an eval report.

BOTH CONSTANTS ARE UPDATED BY HAND, TOGETHER. That is the whole point: the
digest is not computed from the tree at test time and compared with itself, it
is a value somebody wrote down when they decided the stamp had moved.
"""

from __future__ import annotations

import hashlib

from dodeal_ai.core import prompting
from dodeal_ai.units.structured_intelligence.pipeline import PROMPT_SET_VERSION
from dodeal_ai.units.structured_intelligence.templates import UNIT_A_TEMPLATES

# The stamp this digest belongs to, and the digest of the ten template texts
# under it. Move BOTH, in one commit, whenever a prompt file changes: the pair
# is the claim that a judgement stamped this string can be reproduced from
# these texts. A stale value here fails this test and nothing else.
PROMPT_SET = "unit_a_prompts_v3"
PROMPT_SET_DIGEST = "05a5e8072a7db3647636b88a30517fdcf7215bf73ee16a011f5a1ca343112a0b"

_HOW_TO_UPDATE = (
    "A Unit A prompt template changed. Bump PROMPT_SET_VERSION in pipeline.py "
    "and update PROMPT_SET and PROMPT_SET_DIGEST here, in the same commit."
)


def _digest() -> str:
    """sha256 over every template name and its text, in tuple order.

    THE NAME IS HASHED TOO, so a template joining or leaving `UNIT_A_TEMPLATES`
    moves the digest even when no file's bytes changed. The NUL separators stop
    a rename from being absorbed by the text that follows it.

    THE TEXT IS STRIPPED, exactly as `_read_template_file` strips it, so the
    digest covers what a model is actually sent. A trailing newline an editor
    added is not a prompt change and does not demand a version bump.

    Read from `_DEFAULT_PROMPTS_DIR` and not `_prompts_dir()`: the claim is
    about the files this repository ships, not about whatever a local
    DODEAL_PROMPTS_DIR points at.
    """
    digest = hashlib.sha256()
    for name in UNIT_A_TEMPLATES:
        text = (prompting._DEFAULT_PROMPTS_DIR / name).read_text(encoding="utf-8")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def test_the_prompt_set_digest_matches_its_stamp() -> None:
    """The ten shipped templates hash to the digest pinned for this stamp."""
    assert PROMPT_SET_VERSION == PROMPT_SET, _HOW_TO_UPDATE
    assert _digest() == PROMPT_SET_DIGEST, _HOW_TO_UPDATE
