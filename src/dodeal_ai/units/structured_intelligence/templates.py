"""Every prompt template Unit A can send, as one tuple the lifespan preloads.

WHY THE UNIT OWNS THIS LIST AND core/ DOES NOT. `core/prompting.py` knows how to
read a template; it has never known which ones exist, and a hardcoded list of
ten filenames there would be a second place to edit every time this unit gains
a pass. The unit names them, main.py hands them to core at startup.

BUILT FROM THE CONSTANTS, NEVER RETYPED. Each entry is the same object the
calling module passes to `build_prompt`, so a renamed file or a moved constant
cannot leave this tuple naming a template nothing sends -- or, worse, leave a
template that IS sent out of the preload and reading from disk on a paid call.

WHY IT LIVES HERE AND NOT IN __init__.py. That file is deliberately empty (see
its docstring), and filling it would make importing ANY module in this package
-- `state`, which the root conftest imports -- drag in classify, vague, scoring
and llm_call with it. A separate module keeps the import graph as it is.
"""

from __future__ import annotations

from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.llm_call import REPROMPT_TAIL_TEMPLATE
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import (
    VAGUE_SHARED_TEMPLATE,
    template_for,
)

# The six types the vague pass runs on, derived from the enum rather than
# listed: an eighth NoteType then appears here the moment it is added, and its
# missing template is a PromptError at import instead of on a paid call.
# system_event is excluded because the classifier suppresses it before pass two.
VAGUE_CHECKED_TYPES: tuple[NoteType, ...] = tuple(
    note_type for note_type in NoteType if note_type is not NoteType.SYSTEM_EVENT
)

# The ten, in pipeline order: classify, the shared vague block, the six vague
# type blocks, score, and the reprompt tail every pass can append. A tuple, not
# a set: the preload's error names the first missing file, and a stable order
# makes that reproducible.
UNIT_A_TEMPLATES: tuple[str, ...] = (
    CLASSIFY_TEMPLATE,
    VAGUE_SHARED_TEMPLATE,
    *(template_for(note_type) for note_type in VAGUE_CHECKED_TYPES),
    SCORE_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
)
