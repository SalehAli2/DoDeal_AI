"""One side's language fallback (D-78): a failed language quote sends only
that side to the script, read from its own segments; the other side keeps its
verified language, through the stored block, stage 2's read-back and the
coaching gate. With both quotes failed the whole call falls back as before."""

from __future__ import annotations

import pytest

from dodeal_ai.units.call_intelligence.coaching import agent_dialect
from dodeal_ai.units.call_intelligence.language import (
    UNHEARD,
    Spoken,
    call_profile,
    coached,
    languages_block,
    message_language,
    spoken_of,
    summary_language,
)
from dodeal_ai.units.call_intelligence.roles import (
    apply_roles,
    check_roles,
    judge,
    opening,
    spoken,
)
from dodeal_ai.units.call_intelligence.transcriber import LanguageProfile, Transcript
from tests.unit.test_call_languages import _answer, _call, _say, _side

# An invented call: the agent speaks Gulf Arabic for most of it, the client
# answers once in English. By time the whole call is mostly Arabic.
CALL = _call(
    _say(0, "speaker_1", "هلا والله، معك مكتب المبيعات عن الفيلا", "ar"),
    _say(5, "speaker_2", "Yes, I want a villa near the sea", "en"),
    _say(10, "speaker_1", "الفيلا فيها خمس غرف وحديقة كبيرة", "ar"),
    _say(15, "speaker_1", "والسعر مناسب جدا لهالمنطقة", "ar"),
    _say(20, "speaker_1", "نقدر نرتب معاينة يوم الثلاثاء", "ar"),
    _say(25, "speaker_1", "نشوفك باجر ان شاء الله", "ar"),
)
AGENT_OWN = _side("gulf_ar", "معك مكتب المبيعات عن الفيلا", "s1")
CLIENT_OWN = _side("en", "I want a villa", "s2")
# Each language quoted from the other side's voice: the quote fails.
CLIENT_FROM_AGENT = _side("en", "هلا والله", "s1")
AGENT_FROM_CLIENT = _side("gulf_ar", "Yes", "s2")
ARABIC_ONLY = frozenset({"gulf_ar"})
ENGLISH_ONLY = frozenset({"en"})


def _judged(client: dict, agent: dict) -> tuple[Spoken, Transcript]:
    """Checked, applied, each side's language as code keeps it, and the
    transcript with its roles, as stage 1 stores it."""
    answer = _answer(CALL, client, agent)
    view = opening(CALL, country_code="971")
    check_roles(view)(answer)
    judged = judge(view, answer)
    relabelled, block = apply_roles(CALL, judged, call_seconds=150)
    assert block["applied"] is True
    return spoken(judged, applied=True), relabelled


def test_the_clients_failed_quote_sends_only_the_client_to_its_script() -> None:
    """The guard: the agent keeps gulf_ar; the client is English by its own
    segment, so the call is mixed, and a tenant coaching Arabic alone does not
    coach it -- whatever the whole call's time says."""
    heard, transcript = _judged(CLIENT_FROM_AGENT, AGENT_OWN)

    assert heard == Spoken(agent="gulf_ar", script="client")
    assert CALL.language_profile is LanguageProfile.MOSTLY_AR
    assert call_profile(transcript, heard) is LanguageProfile.MIXED
    assert summary_language(transcript, heard) == "ar"
    assert agent_dialect(heard) == "gulf_ar"
    block = languages_block(transcript, heard)
    assert block == {
        "client": None,
        "agent": "gulf_ar",
        "profile": "mixed",
        "source": "model_and_script",
    }
    back = spoken_of(block)
    assert back == heard
    assert coached(back, transcript.segments, ARABIC_ONLY) is False
    assert coached(back, transcript.segments, ARABIC_ONLY | ENGLISH_ONLY) is True


def test_the_agents_failed_quote_sends_only_the_agent_to_its_script() -> None:
    heard, transcript = _judged(CLIENT_OWN, AGENT_FROM_CLIENT)

    assert heard == Spoken(client="en", script="agent")
    assert message_language(heard.client, "ar") == "en"
    assert languages_block(transcript, heard)["profile"] == "mixed"
    # The agent's own segments are Arabic: any Arabic code lists them.
    assert coached(heard, transcript.segments, ENGLISH_ONLY) is False
    assert coached(heard, transcript.segments, frozenset({"en", "msa_ar"})) is True


def test_both_quotes_failed_the_whole_call_falls_back() -> None:
    heard, transcript = _judged(CLIENT_FROM_AGENT, AGENT_FROM_CLIENT)

    assert heard == UNHEARD
    assert languages_block(transcript, heard) == {
        "client": None,
        "agent": None,
        "profile": "mostly_ar",
        "source": "script",
    }
    assert coached(heard, transcript.segments, ARABIC_ONLY) is True


@pytest.mark.parametrize(
    "block",
    [
        {"client": None, "agent": None, "source": "model_and_script"},
        {"client": "en", "agent": "gulf_ar", "source": "model_and_script"},
        {"client": None, "agent": "xx", "source": "model_and_script"},
    ],
    ids=["no-side-kept", "no-side-null", "unknown-code"],
)
def test_a_one_sided_block_needs_exactly_one_known_side(block: dict) -> None:
    assert spoken_of(block) == UNHEARD


def test_a_script_side_with_no_segments_of_its_own_is_never_coached() -> None:
    lone = Spoken(agent="gulf_ar", script="client")
    arabic = (CALL.segments[0],)
    assert coached(lone, arabic, ARABIC_ONLY | ENGLISH_ONLY) is False
