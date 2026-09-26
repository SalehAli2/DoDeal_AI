"""Each side's language by the model (roles.py's call_languages, language.py):
quoted from that side's own voice, read by the language profile and the
summary language instead of the script, which stays only as the fallback."""

from __future__ import annotations

from typing import Any

import pytest

from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.language import (
    UNHEARD,
    Spoken,
    call_profile,
    languages_block,
    spoken_of,
    summary_language,
)
from dodeal_ai.units.call_intelligence.roles import (
    Roles,
    apply_roles,
    check_roles,
    opening,
    spoken,
)
from dodeal_ai.units.call_intelligence.transcriber import (
    LanguageProfile,
    Segment,
    Transcript,
)
from dodeal_ai.units.call_intelligence.worker import process_call
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_roles import ctx  # noqa: F401  (the stage-1 fixture)
from tests.unit.test_call_stage1 import EXTRACTION, JOB, PROSE, _push, _result


def _say(start: float, speaker: str, text: str, language: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language=language,
        confidence=0.9,
    )


def _call(*segments: Segment) -> Transcript:
    return Transcript.of(segments, provider="recorded", model="recorded-stt-1")


# Invented calls, each labelled by an engine that names a language by script:
# Urdu in Arabic letters reads as ar, French in Latin letters as en.
URDU = _call(
    _say(0, "speaker_1", "السلام علیکم، میں کمپنی سے آپ کے ولا کے بارے میں", "ar"),
    _say(5, "speaker_2", "جی، مجھے سمندر کے قریب ولا چاہیے", "ar"),
)
FRENCH = _call(
    _say(0, "speaker_1", "Bonjour, je vous appelle de la société pour la villa", "en"),
    _say(5, "speaker_2", "Oui, je cherche une villa près de la mer", "en"),
)


def _side(language: str | None, quote: str | None, segment: str | None) -> dict:
    return {"language": language, "quote": quote, "segment": segment}


def _roles(client: dict, agent: dict) -> dict[str, Any]:
    """speaker_1 the agent and speaker_2 the client, as each opening shows."""
    return {
        "speakers": [
            {
                "speaker": "speaker_1",
                "role": "agent",
                "quote": None,
                "segment": None,
            },
            {
                "speaker": "speaker_2",
                "role": "client",
                "quote": None,
                "segment": None,
            },
        ],
        "call_languages": {"client": client, "agent": agent},
    }


def _answer(transcript: Transcript, client: dict, agent: dict) -> Roles:
    """The answer with each voice's role quoted from its own first segment."""
    raw = _roles(client, agent)
    for n, voice in enumerate(raw["speakers"]):
        voice["quote"] = transcript.segments[n].text.split("،")[0].split(",")[0]
        voice["segment"] = f"s{n + 1}"
    return Roles.model_validate(raw)


URDU_ANSWER = (
    _side("ur", "مجھے سمندر کے قریب ولا چاہیے", "s2"),
    _side("ur", "میں کمپنی سے آپ کے ولا کے بارے میں", "s1"),
)
FRENCH_ANSWER = (
    _side("fr", "je cherche une villa près de la mer", "s2"),
    _side("fr", "je vous appelle de la société", "s1"),
)


def _heard(transcript: Transcript, client: dict, agent: dict) -> Spoken:
    """Checked, applied, and each side's language as code keeps it."""
    answer = _answer(transcript, client, agent)
    check_roles(opening(transcript, country_code="971"))(answer)
    _, block = apply_roles(transcript, answer, call_seconds=150)
    assert block["applied"] is True
    return spoken(answer, applied=True)


def _refused(transcript: Transcript, client: dict, agent: dict):
    with pytest.raises(OutputValidationError) as refused:
        check_roles(opening(transcript, country_code="971"))(
            _answer(transcript, client, agent)
        )
    return refused.value.errors


# --- the guard: the language is the model's, never the script's ---------------------


def test_an_urdu_call_is_ur_not_arabic() -> None:
    assert URDU.language_profile is LanguageProfile.MOSTLY_AR
    heard = _heard(URDU, *URDU_ANSWER)
    assert heard == Spoken(client="ur", agent="ur")
    assert call_profile(URDU, heard) is LanguageProfile.OTHER
    assert CallText.of(URDU, country_code="971", spoken=heard).language == "en"
    assert languages_block(URDU, heard) == {
        "client": "ur",
        "agent": "ur",
        "profile": "other",
        "source": "model",
    }


def test_a_french_call_is_fr_not_en() -> None:
    assert FRENCH.language_profile is LanguageProfile.MOSTLY_EN
    heard = _heard(FRENCH, *FRENCH_ANSWER)
    assert heard == Spoken(client="fr", agent="fr")
    assert call_profile(FRENCH, heard) is LanguageProfile.OTHER
    assert languages_block(FRENCH, heard)["profile"] == "other"


def test_a_side_quoted_from_the_other_sides_voice_is_malformed() -> None:
    client, agent = URDU_ANSWER
    borrowed = _side("ur", agent["quote"], agent["segment"])
    assert _refused(URDU, borrowed, agent) == (
        ("call_languages.client", "quote_wrong_speaker"),
    )
    assert _refused(URDU, client, _side("ur", client["quote"], "s2")) == (
        ("call_languages.agent", "quote_wrong_speaker"),
    )
    assert _refused(URDU, _side("ur", None, None), agent) == (
        ("call_languages.client", "quote_missing"),
    )
    assert _refused(URDU, client, _side(None, client["quote"], "s2")) == (
        ("call_languages.agent", "quote_wrong_speaker"),
    )


# --- the fallback and the rest ----------------------------------------------------------


def test_with_nothing_heard_the_script_decides_as_before() -> None:
    assert summary_language(URDU, UNHEARD) == "ar"
    assert call_profile(FRENCH) is LanguageProfile.MOSTLY_EN
    assert languages_block(URDU, UNHEARD) == {
        "client": None,
        "agent": None,
        "profile": "mostly_ar",
        "source": "script",
    }


@pytest.mark.parametrize(
    ("client", "agent", "profile", "language"),
    [
        ("gulf_ar", "egyptian_ar", LanguageProfile.MOSTLY_AR, "ar"),
        ("en", None, LanguageProfile.MOSTLY_EN, "en"),
        ("en", "levantine_ar", LanguageProfile.MIXED, "en"),
        ("tr", "gulf_ar", LanguageProfile.OTHER, "en"),
        (None, "msa_ar", LanguageProfile.MOSTLY_AR, "ar"),
    ],
)
def test_the_profile_and_summary_follow_the_sides_heard(
    client: str | None, agent: str | None, profile: LanguageProfile, language: str
) -> None:
    heard = Spoken(client=client, agent=agent)
    assert call_profile(FRENCH, heard) is profile
    assert summary_language(FRENCH, heard) == language


def test_an_unapplied_mapping_or_no_answer_hears_nothing() -> None:
    answer = _answer(URDU, *URDU_ANSWER)
    assert spoken(answer, applied=False) == UNHEARD
    assert spoken(None, applied=True) == UNHEARD


def test_an_answer_kept_before_the_languages_reads_back_as_unheard() -> None:
    raw = _roles(*URDU_ANSWER)
    del raw["call_languages"]
    kept = Roles.model_validate(raw)
    assert spoken(kept, applied=True) == UNHEARD


@pytest.mark.parametrize(
    "block",
    [
        None,
        {"client": "ur", "agent": "ur", "profile": "other", "source": "script"},
        {"client": "xx", "agent": None, "profile": "other", "source": "model"},
    ],
    ids=["missing", "fallback", "unknown-code"],
)
def test_stage_2_reads_back_only_a_model_block_of_known_codes(block: object) -> None:
    assert spoken_of(block) == UNHEARD
    stored = languages_block(URDU, Spoken(client="ur", agent=None))
    assert spoken_of(stored) == Spoken(client="ur", agent=None)


async def test_stage_1_carries_the_languages_block(ctx: dict[str, Any]) -> None:  # noqa: F811
    english = {
        "speakers": [
            {
                "speaker": "speaker_1",
                "role": "agent",
                "quote": "this is the sales office",
                "segment": "s1",
            },
            {
                "speaker": "speaker_2",
                "role": "client",
                "quote": "I want a villa",
                "segment": "s2",
            },
        ],
        "call_languages": {
            "client": _side("en", "I want a villa", "s2"),
            "agent": _side("en", "this is the sales office", "s1"),
        },
    }
    ctx["llm"] = FakeLLM(
        json_response(english), json_response(EXTRACTION), json_response(PROSE)
    )
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    result = await _result()
    assert result["languages"] == {
        "client": "en",
        "agent": "en",
        "profile": "mostly_en",
        "source": "model",
    }
    assert result["roles"]["call_languages"] == english["call_languages"]
    assert result["analysis"]["language"] == "en"
