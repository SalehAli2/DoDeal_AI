"""A voice no role mapping named (A2): written unknown in every prompt that
can read it, never as the client; a check that names a speaker fails on it,
without making the answer malformed."""

from __future__ import annotations

from dodeal_ai.core.prompting import build_prompt
from dodeal_ai.units.call_intelligence.evidence import (
    SPEAKER_UNKNOWN,
    CallText,
    quote_errors,
)
from dodeal_ai.units.call_intelligence.passes import (
    Extraction,
    check_extraction,
    settled,
)
from dodeal_ai.units.call_intelligence.prompts import (
    AGENT,
    CLIENT,
    EXTRACT_TEMPLATE,
    PROSE_TEMPLATE,
    WHATSAPP_TEMPLATE,
    render_transcript,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from tests.unit.test_call_loss_reason import _dead, _lost


def _say(start: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


def _call(first: str, second: str) -> CallText:
    """An invented dead call, its two voices labelled `first` and `second`."""
    segments = (
        _say(0, first, "Good morning, this is the sales office about the villa."),
        _say(5, second, "Sorry, the price is too high for me."),
    )
    return CallText.of(
        Transcript.of(segments, provider="fake", model="fake"), country_code="971"
    )


def test_an_unmapped_voice_is_written_unknown_never_client() -> None:
    """The guard (A2): the engine's labels left as they were, and unknown, are
    written unknown; agent and every other label as before."""
    segments = tuple(
        _say(5 * n, label, "hello")
        for n, label in enumerate(
            ("speaker_1", "speaker_2", "agent", "lead", "unknown")
        )
    )
    shown = [
        line.split("]")[0].split()[-1]
        for line in render_transcript(segments).splitlines()
    ]
    assert shown == ["unknown", "unknown", "agent", "client", "unknown"]
    assert "[s1 00:00 unknown] Good morning" in _call("speaker_1", "speaker_2").data()


def test_every_prompt_that_can_read_an_unmapped_call_names_unknown() -> None:
    for template in (EXTRACT_TEMPLATE, PROSE_TEMPLATE, WHATSAPP_TEMPLATE):
        text = " ".join(build_prompt(template, caller_data="").stable.split())
        assert "speaker is agent, client or unknown" in text
        assert "never take it for the agent or the client" in text or (
            "never to be taken for the agent or the client" in text
        )


def test_a_check_naming_a_speaker_fails_on_an_unknown_voice() -> None:
    unmapped, mapped = _call("speaker_1", "speaker_2"), _call("agent", "lead")
    quote = "the price is too high"
    assert quote_errors(unmapped, "x", quote, "s2", speaker=CLIENT) == [
        ("x", SPEAKER_UNKNOWN)
    ]
    assert quote_errors(mapped, "x", quote, "s2", speaker=CLIENT) == []
    assert quote_errors(mapped, "x", quote, "s2", speaker=AGENT) == [
        ("x", "quote_wrong_speaker")
    ]
    assert quote_errors(unmapped, "x", quote, "s2") == []


def test_a_loss_reason_from_an_unknown_voice_is_unverified_never_malformed() -> None:
    """Its only quote failing on an unknown voice, the extraction is kept and
    the loss reason unverified: no reprompt is paid for a voice nobody named."""
    answer = _dead(_lost("price", "the price is too high", "s2"))
    answer["mood"] = {"value": "negative", "quote": None, "segment": None}
    extraction = Extraction.model_validate(answer)
    unmapped = _call("speaker_1", "speaker_2")
    check_extraction(unmapped)(extraction)
    kept = settled(extraction, unmapped)["loss_reason"]
    assert (kept["unverified"], kept["quote"]) == (True, None)
    mapped = settled(extraction, _call("agent", "lead"))["loss_reason"]
    assert mapped["unverified"] is False
