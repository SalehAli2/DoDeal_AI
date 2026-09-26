"""One failure never takes more than its own share (A3): the roles not applied
leave each detail its own verified state, the call still uncertain."""

from __future__ import annotations

import pytest

from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.passes import Extraction, settled
from dodeal_ai.units.call_intelligence.transcriber import Transcript
from tests.unit.test_call_passes import BUDGET, SEGMENTS, _extraction

LABELS = {"agent": "speaker_1", "lead": "speaker_2"}


def _call(*reasons: str) -> CallText:
    """The invented call as the engine labelled it, doubted for `reasons`."""
    segments = tuple(
        s.model_copy(update={"speaker": LABELS[s.speaker]}) for s in SEGMENTS
    )
    transcript = Transcript.of(segments, provider="f", model="f", reasons=reasons)
    return CallText.of(transcript, country_code="971")


@pytest.mark.parametrize(
    ("reasons", "state"),
    [
        (("roles_failed",), "stated"),
        (("roles_unclear",), "stated"),
        (("speakers_over_two", "roles_unclear"), "uncertain"),
        (("roles_failed", "low_volume"), "uncertain"),
    ],
)
def test_the_roles_not_applied_leave_each_detail_its_own_state(
    reasons: tuple[str, ...], state: str
) -> None:
    """The guard (A3): a transcript doubted only because its roles were not
    applied has its words as heard, so a verified detail stays stated; the
    call and the client's mood stay uncertain. Any other doubt is the words'."""
    call = _call(*reasons)
    kept = settled(Extraction.model_validate(_extraction(budget=BUDGET)), call)
    assert call.uncertain is True
    assert kept["details"]["budget"]["state"] == state
    assert kept["details"]["budget"]["evidence_failed"] is False
    assert kept["mood"]["uncertain"] is True
