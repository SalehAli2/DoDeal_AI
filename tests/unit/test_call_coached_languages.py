"""unit_b.coaching_languages (BRD B1): a call whose client's language is not
listed gets no coaching and no score, with language_not_enabled, and neither
pass is paid for (language.coached, wave2.py)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.jobs import JobStatus, Stage2State, create_job, read_job, transition
from dodeal_ai.units.call_intelligence.config import CallsConfig, parse_unit_b_section
from dodeal_ai.units.call_intelligence.language import UNHEARD, Spoken, coached
from dodeal_ai.units.call_intelligence.paid import PassUsage
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    EXTRAS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.wave2 import COACHING, SCORE, wave2
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.wave2_answers import coaching_answer, extras_answer

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")
NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)


def _say(start: float, speaker: str, text: str, language: str = "en") -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language=language,
        confidence=0.9,
    )


# An invented call; its words matter only to the passes the gate lets run.
SEGMENTS = (
    _say(0, "agent", "Good morning, calling about the villa you asked for."),
    _say(5, "lead", "Yes, I want a villa with a garden near the sea."),
)
TRANSCRIPT = Transcript.of(SEGMENTS, provider="fake", model="fake")


async def _wave2(config: CallsConfig, spoken: Spoken, *, eligible: bool = False):
    await create_job(
        "tenant-a",
        7,
        job_id="job-1",
        request_id="req-1",
        queue="arq:calls:normal",
        metadata={"author_id": 27, "duration_seconds": 150},
        now=NOW,
    )
    job = await read_job("tenant-a", "job-1")
    assert job is not None
    await transition(
        job, JobStatus.DONE, now=NOW, ttl_seconds=600, stage2=Stage2State.PENDING
    )
    llm = FakeLLM()
    llm.script_for(OBJECTIONS_TEMPLATE, json_response({"objections": []}))
    llm.script_for(ESCALATIONS_TEMPLATE, json_response({"escalations": []}))
    llm.script_for(COACHING_TEMPLATE, json_response(coaching_answer(SEGMENTS[0].text)))
    llm.script_for(EXTRAS_TEMPLATE, json_response(extras_answer()))
    wave = await wave2(
        llm,
        await read_job("tenant-a", "job-1"),
        config,
        TRANSCRIPT,
        work={},
        scope=SCOPE,
        settings=get_settings(),
        usage=PassUsage(),
        eligible=eligible,
        stage1_escalations=[],
        spoken=spoken,
    )
    return llm, wave


def _gated(wave) -> dict[str, str | None]:
    """The coaching and score reasons; the extras pass is another test's."""
    return {name: wave.reasons.get(name) for name in (COACHING, SCORE)}


# --- the guard ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", ["ur", "hi", "fr", "other", "maghrebi_ar"])
async def test_an_unlisted_client_language_gets_no_coaching_and_no_score(
    client: str,
) -> None:
    llm, wave = await _wave2(
        CallsConfig(scoring_enabled=True), Spoken(client=client, agent="en")
    )
    assert (wave.parts[COACHING], wave.parts[SCORE]) == (None, None)
    assert _gated(wave) == {
        COACHING: "language_not_enabled",
        SCORE: "language_not_enabled",
    }
    assert "unit_b.coaching" not in llm.profiles
    assert "unit_b.score" not in llm.profiles


@pytest.mark.parametrize("client", ["gulf_ar", "iraqi_ar", "en"])
async def test_a_listed_client_language_is_coached_and_gated_as_before(
    client: str,
) -> None:
    llm, wave = await _wave2(
        CallsConfig(scoring_enabled=True), Spoken(client=client, agent="en")
    )
    assert wave.parts[COACHING] is not None
    assert _gated(wave) == {COACHING: None, SCORE: "not_eligible"}
    assert "unit_b.coaching" in llm.profiles


async def test_a_language_the_tenant_lists_is_coached() -> None:
    config = CallsConfig(coaching_languages=frozenset({"ur"}))
    _, wave = await _wave2(config, Spoken(client="ur", agent="en"))
    assert wave.parts[COACHING] is not None
    assert _gated(wave) == {COACHING: None, SCORE: "scoring_off"}


async def test_scoring_off_stays_the_score_reason() -> None:
    _, wave = await _wave2(CallsConfig(), Spoken(client="ru", agent="en"))
    assert _gated(wave) == {COACHING: "language_not_enabled", SCORE: "scoring_off"}


# --- the fallback when no side was heard -------------------------------------------------


def _profile(*languages: str) -> tuple[Segment, ...]:
    return tuple(
        _say(n * 5, "agent", "word", language) for n, language in enumerate(languages)
    )


@pytest.mark.parametrize(
    ("languages", "enabled", "expected"),
    [
        (("en", "en"), {"en"}, True),
        (("en", "en"), {"gulf_ar"}, False),
        (("ar", "ar"), {"iraqi_ar"}, True),
        (("ar", "ar"), {"en"}, False),
        (("ar", "en"), {"en", "gulf_ar"}, True),
        (("ar", "en"), {"en"}, False),
        (("ur", "ur"), {"en", "gulf_ar", "ur"}, False),
    ],
    ids=["en", "en-unlisted", "ar", "ar-unlisted", "mixed", "mixed-half", "other"],
)
def test_unheard_the_script_profile_decides(
    languages: tuple[str, ...], enabled: set[str], expected: bool
) -> None:
    assert coached(UNHEARD, _profile(*languages), frozenset(enabled)) is expected


def test_a_heard_client_decides_whatever_the_script() -> None:
    arabic = _profile("ar", "ar")
    assert coached(Spoken(client="ur"), arabic, frozenset({"gulf_ar"})) is False
    assert coached(Spoken(client="ur"), arabic, frozenset({"ur"})) is True


# --- the setting -------------------------------------------------------------------------


def test_the_default_is_the_four_arabic_dialects_and_english() -> None:
    assert CallsConfig().coaching_languages == frozenset(
        {"gulf_ar", "egyptian_ar", "levantine_ar", "iraqi_ar", "en"}
    )
    parsed = parse_unit_b_section({"coaching_languages": ["ur", "en"]})
    assert parsed.coaching_languages == frozenset({"ur", "en"})
    for refused in (["other"], ["arabic"], ["EN"], "en"):
        with pytest.raises(ValidationError):
            parse_unit_b_section({"coaching_languages": refused})
