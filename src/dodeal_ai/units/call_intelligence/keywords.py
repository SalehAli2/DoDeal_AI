"""The tenant's keyword vocabulary (Unit B): its canonical names for projects,
communities and developers, used AFTER transcription and never sent to the
speech-to-text engine.

SPOTTED IN CODE, with no model: a listed term is in a segment when its words,
normalised as the alarm matcher normalises (alarms.py), appear there one
right after another, each allowed the proclitics the alarm matcher allows. No
gap is allowed inside a name. One find per term per segment, {term, segment,
start_s}, the term exactly as the tenant listed it. The extras pass then maps
its own keywords onto the same list (extras.py).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from dodeal_ai.units.call_intelligence.alarms import same_word, words
from dodeal_ai.units.call_intelligence.prompts import segment_id
from dodeal_ai.units.call_intelligence.transcriber import Segment

# The most terms a tenant may list: a company's projects and developers, not
# a dictionary. Bounds the matcher's work per segment.
MAX_KEYWORD_TERMS = 100


def term_in(term: Sequence[str], said: Sequence[str]) -> bool:
    """Whether the term's words appear in `said` unbroken, in order."""
    size = len(term)
    return bool(term) and any(
        all(same_word(said[at + n], word) for n, word in enumerate(term))
        for at in range(len(said) - size + 1)
    )


def spot_keywords(
    segments: Sequence[Segment], vocabulary: Iterable[str]
) -> list[dict[str, object]]:
    """Every listed term in every segment, in segment order, then term order."""
    wanted = [(term, words(term)) for term in sorted(vocabulary)]
    finds: list[dict[str, object]] = []
    for index, segment in enumerate(segments):
        said = words(segment.text)
        finds += [
            {"term": term, "segment": segment_id(index), "start_s": segment.start_s}
            for term, split in wanted
            if term_in(split, said)
        ]
    return finds
