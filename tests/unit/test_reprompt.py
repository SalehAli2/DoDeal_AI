"""The single reprompt: what the second prompt is, what it costs, and what it
is never allowed to carry.

A malformed answer buys one more attempt and no more. The attempt is not a
retry -- the same prompt sent again would be the same question asked twice -- it
is the SAME stable template and the SAME caller data with a trusted tail
appended, saying in more words than the template does to answer with the object
and nothing around it.

Three things are load-bearing and each has a test that fails loudly if it moves:

  1. `.stable` and `.variable` are byte-identical across the two calls, so the
     only difference is the tail. Anything else and the cached prefix misses and
     the second answer is not an answer to the first question.
  2. The rejected output appears in NOTHING: not the second prompt, not the
     error, not a log line. It is untrusted text that a stranger's note may have
     shaped, and the whole prompt boundary exists to keep it outside.
  3. Two calls, then 503. There is no third attempt and no loop to bound.

The per-task output ceilings (register item 15) are here too, because
truncation is the failure they exist to prevent and this file is where
truncation is malformed.
"""

from __future__ import annotations

import json

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import MalformedOutputError, ModelUnavailableError
from dodeal_ai.core.llm import LLMErrorReason, LLMProviderError
from dodeal_ai.core.prompting import _load_template
from dodeal_ai.schemas.lead import Lead
from dodeal_ai.units.structured_intelligence.classify import (
    CLASSIFY_MAX_OUTPUT_TOKENS,
    classify,
)
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.llm_call import REPROMPT_TAIL_TEMPLATE
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import (
    SCORE_MAX_OUTPUT_TOKENS,
    score_note,
)
from dodeal_ai.units.structured_intelligence.vague import (
    VAGUE_MAX_OUTPUT_TOKENS,
    detect_vagueness,
)
from tests.helpers.fake_leads import note
from tests.helpers.fake_llm import FakeLLM, json_response, response, truncated
from tests.helpers.scopes import TEST_SCOPE
from tests.helpers.score_answers import score_payload

CONFIG = get_tenant_config("tenant-a")
NOTE_TEXT = "Called the client about the New Cairo 3BR; calling back Tuesday."

GOOD_CLASSIFICATION = {"note_type": "discovery"}
GOOD_CHECKS = score_payload()
GOOD_VAGUE = {
    "is_vague": True,
    "missing_components": ["next_step_with_date"],
    "clarification_prompt": "Which Tuesday, and what will you cover?",
    "reasoning": "The follow-up has no date.",
}


def _note(text: str = NOTE_TEXT):
    return note(10, text)


def _lead() -> Lead:
    return Lead(id=1656)


async def _classify(*script):
    client = FakeLLM(*script)
    output, llm_response = await classify(
        client, _note(), _lead(), scope=TEST_SCOPE, settings=get_settings()
    )
    return output, llm_response, client


# --- one call when the answer is good, two when it is not -------------------


async def test_a_good_answer_costs_one_call():
    # The second scripted answer is there to be NOT used: if a valid answer ever
    # earned a reprompt, this count would say so.
    _, _, client = await _classify(
        json_response(GOOD_CLASSIFICATION), json_response(GOOD_CLASSIFICATION)
    )
    assert client.call_count == 1


async def test_a_bad_answer_then_a_good_one_costs_two_calls():
    output, _, client = await _classify(
        response("Sure! It is a discovery note."), json_response(GOOD_CLASSIFICATION)
    )
    assert client.call_count == 2
    assert output.note_type == NoteType.DISCOVERY


async def test_two_bad_answers_end_in_malformed_output():
    client = FakeLLM(
        response("not json"),
        response("still not json"),
        json_response(GOOD_CLASSIFICATION),
    )
    with pytest.raises(MalformedOutputError) as raised:
        await classify(
            client, _note(), _lead(), scope=TEST_SCOPE, settings=get_settings()
        )

    # Two, not three: the third scripted answer is a valid one the model never
    # gets to give, so a loop would show up here as a pass.
    assert client.call_count == 2
    assert raised.value.http_status == 503
    assert raised.value.reason_code == "malformed_output"


async def test_a_provider_failure_on_the_reprompt_is_model_unavailable():
    """The second call is a REAL call and can fail like any other.

    A malformed first answer earns the reprompt; the provider then does not
    answer at all. Those are different failures with different fixes, so the
    caller is told what actually happened last -- model_unavailable, "try
    again" -- and not malformed_output, which would send them looking at a
    model that never got to reply.

    Two calls, not three: the reprompt is the ONE recovery, and it does not
    earn a recovery of its own.
    """
    client = FakeLLM(
        response("not json"),
        LLMProviderError(LLMErrorReason.UNAVAILABLE, transient=True),
        json_response(GOOD_CLASSIFICATION),
    )
    with pytest.raises(ModelUnavailableError) as raised:
        await classify(
            client, _note(), _lead(), scope=TEST_SCOPE, settings=get_settings()
        )

    assert client.call_count == 2
    assert raised.value.reason_code == "model_unavailable"
    assert raised.value.http_status == 503


async def test_the_judgement_is_stamped_with_the_call_it_came_from():
    # The reprompt is a second paid call to a provider that may report a
    # different model than the first did. What is stamped is what ANSWERED.
    _, llm_response, _ = await _classify(
        response("not json", model="model-that-failed"),
        json_response(GOOD_CLASSIFICATION, model="model-that-answered"),
    )
    assert llm_response.model == "model-that-answered"


# --- the second prompt: one difference, and it is the tail ------------------


async def test_the_second_prompt_differs_only_in_the_tail():
    _, _, client = await _classify(
        response("not json"), json_response(GOOD_CLASSIFICATION)
    )
    first, second = client.prompts

    assert second.stable == first.stable
    assert second.variable == first.variable
    assert first.tail == ""
    assert second.tail


async def test_the_tail_is_the_versioned_file_verbatim():
    _, _, client = await _classify(
        response("not json"), json_response(GOOD_CLASSIFICATION)
    )
    assert client.prompts[1].tail == _load_template(REPROMPT_TAIL_TEMPLATE)


async def test_the_tail_is_rendered_after_the_caller_data():
    # It is a TRAILING instruction: the untrusted section is closed before it
    # starts, so the tail can never be read as part of the note.
    _, _, client = await _classify(
        response("not json"), json_response(GOOD_CLASSIFICATION)
    )
    second = client.prompts[1]
    assert second.text.index("----- END CALLER DATA -----") < second.text.index(
        second.tail
    )


async def test_the_rejected_output_is_not_in_the_second_prompt():
    # The one thing that must never be fed back. It is model output shaped by a
    # note we did not write, and putting it in a prompt would carry it across
    # the boundary this repo exists to keep.
    rejected = "I think this note is about SENTINEL-0501234567 villa budget 4.2M"
    _, _, client = await _classify(
        response(rejected), json_response(GOOD_CLASSIFICATION)
    )
    second = client.prompts[1]

    assert rejected not in second.text
    assert "0501234567" not in second.text
    assert "SENTINEL" not in second.text


async def test_the_tail_never_names_what_was_wrong_with_the_answer():
    # Two different failures, one tail. If the tail were built from the
    # rejection it would differ between these, and the difference would be
    # derived from model output.
    _, _, from_prose = await _classify(
        response("not json"), json_response(GOOD_CLASSIFICATION)
    )
    _, _, from_bad_shape = await _classify(
        json_response({"note_type": "site_visit"}), json_response(GOOD_CLASSIFICATION)
    )
    assert from_prose.prompts[1].tail == from_bad_shape.prompts[1].tail


def test_the_tail_does_not_pretend_there_was_a_previous_answer():
    """Every call is a fresh prompt and the model has no history.

    It has not seen an earlier answer, has not been told one was turned down,
    and has read nothing before the prompt it is holding. So a tail that opened
    "YOUR PREVIOUS ANSWER WAS ..." and asked the model to reconsider its
    judgement was describing a conversation that never happened -- and a model
    told to revisit something it never produced has no honest way to comply.

    What the second attempt actually needs is a stricter statement of the FORM
    and nothing about a past. These four words are the ones that carried the
    fiction; they are pinned out so it cannot come back in a rewrite.
    """
    tail = _load_template(REPROMPT_TAIL_TEMPLATE)
    for fiction in ("previous", "rejected", "reconsider", "already"):
        assert fiction not in tail.lower(), fiction

    # The call budget is the one true thing the tail says about the attempt,
    # and it stays last so it is the last thing read.
    assert tail.endswith("This is the second and last attempt. There is no third.")


# --- truncation is malformed ------------------------------------------------


async def test_a_truncated_answer_is_malformed_even_when_it_parses():
    # The dangerous case. MAX_TOKENS means the model stopped mid-sentence, so
    # what came back is the BEGINNING of an answer -- and a fragment that
    # happens to parse and happens to satisfy the schema would otherwise be
    # accepted and stamped on a judgement.
    _, _, client = await _classify(
        truncated(json.dumps(GOOD_CLASSIFICATION)), json_response(GOOD_CLASSIFICATION)
    )
    assert client.call_count == 2


async def test_a_truncated_answer_twice_is_malformed_output():
    cut_off = truncated('{"note_type": "disc')
    client = FakeLLM(cut_off, cut_off)
    with pytest.raises(MalformedOutputError):
        await classify(
            client, _note(), _lead(), scope=TEST_SCOPE, settings=get_settings()
        )
    assert client.call_count == 2


async def test_truncation_is_rejected_by_its_own_error_type(caplog):
    await _classify(
        truncated(json.dumps(GOOD_CLASSIFICATION)), json_response(GOOD_CLASSIFICATION)
    )
    # Not json_invalid: this answer parsed. The distinction is what tells an
    # operator to look at the ceiling rather than at the template.
    assert "error_types=output_truncated" in caplog.text


# --- the reprompt is logged by code, never by content -----------------------


async def test_a_reprompt_logs_the_label_and_nothing_else(caplog):
    rejected = "SENTINEL-0501234567 villa budget 4.2M"
    await _classify(response(rejected), json_response(GOOD_CLASSIFICATION))

    line = next(r for r in caplog.records if r.getMessage() == "reprompt_issued")
    assert line.levelname == "WARNING"
    assert line.reason_code == "reprompt_issued"
    assert line.label == "llm.unit_a.classify"
    assert rejected not in caplog.text
    assert "0501234567" not in caplog.text


async def test_a_good_answer_logs_no_reprompt(caplog):
    await _classify(json_response(GOOD_CLASSIFICATION))
    assert "reprompt_issued" not in caplog.text


# --- every pass gets the reprompt, including the two rule hooks -------------


async def test_vague_detection_reprompts_on_a_malformed_answer():
    client = FakeLLM(response("The note looks thin."), json_response(GOOD_VAGUE))
    output, _ = await detect_vagueness(
        client,
        _note(),
        NoteType.DISCOVERY,
        config=CONFIG,
        scope=TEST_SCOPE,
        settings=get_settings(),
    )
    assert client.call_count == 2
    assert output.is_vague is True


async def test_a_rule_the_schema_cannot_hold_earns_the_reprompt_too():
    # `client_said` for a no_contact note is well-formed JSON that satisfies the
    # schema and breaks a per-type rule. It is malformed in exactly the sense
    # that matters, so it buys the same one attempt as a missing field.
    disallowed = json_response(
        {
            "is_vague": True,
            "missing_components": ["client_said"],
            "clarification_prompt": "What did they say?",
            "reasoning": "Nothing recorded.",
        }
    )
    allowed = json_response(
        {
            "is_vague": True,
            "missing_components": ["next_step_with_date"],
            "clarification_prompt": "When are you trying again?",
            "reasoning": "No next attempt recorded.",
        }
    )
    client = FakeLLM(disallowed, allowed)
    output, _ = await detect_vagueness(
        client,
        _note(),
        NoteType.NO_CONTACT,
        config=CONFIG,
        scope=TEST_SCOPE,
        settings=get_settings(),
    )
    assert client.call_count == 2
    assert [c.value for c in output.missing_components] == ["next_step_with_date"]


async def test_a_check_for_a_suppressed_component_earns_the_reprompt():
    """deal_specifics is off (Q13), so an answer for its check is a malformed one."""
    client = FakeLLM(
        json_response(
            {**GOOD_CHECKS, "checks": {**GOOD_CHECKS["checks"], "ds_figures": True}}
        ),
        json_response(GOOD_CHECKS),
    )
    output, _ = await score_note(
        client,
        _note(),
        NoteType.DISCOVERY,
        config=CONFIG,
        scope=TEST_SCOPE,
        settings=get_settings(),
    )
    assert client.call_count == 2
    assert "ds_figures" not in {check.value for check in output.checks}
    assert len(output.checks) == 9


# --- per-task output ceilings (register item 15) ----------------------------


async def test_each_pass_states_its_own_ceiling():
    _, _, classified = await _classify(json_response(GOOD_CLASSIFICATION))
    assert classified.calls[0].max_output_tokens == CLASSIFY_MAX_OUTPUT_TOKENS

    vague_client = FakeLLM(json_response(GOOD_VAGUE))
    await detect_vagueness(
        vague_client,
        _note(),
        NoteType.DISCOVERY,
        config=CONFIG,
        scope=TEST_SCOPE,
        settings=get_settings(),
    )
    assert vague_client.calls[0].max_output_tokens == VAGUE_MAX_OUTPUT_TOKENS

    score_client = FakeLLM(json_response(GOOD_CHECKS))
    await score_note(
        score_client,
        _note(),
        NoteType.DISCOVERY,
        config=CONFIG,
        scope=TEST_SCOPE,
        settings=get_settings(),
    )
    assert score_client.calls[0].max_output_tokens == SCORE_MAX_OUTPUT_TOKENS


async def test_the_reprompt_carries_the_same_ceiling_as_the_first_call():
    # `max_output_tokens=None` means "the configured default", and a default is
    # what nobody revisits. The tail, not a raised ceiling, is the lever that
    # makes a truncated answer fit the second time.
    _, _, client = await _classify(
        response("not json"), json_response(GOOD_CLASSIFICATION)
    )
    assert [c.max_output_tokens for c in client.calls] == [
        CLASSIFY_MAX_OUTPUT_TOKENS,
        CLASSIFY_MAX_OUTPUT_TOKENS,
    ]


# The sizing rule, restated as arithmetic so that lowering a constant has to
# argue with it. Arabic runs 2-3x the tokens per word that English does; take
# the worst end. An Arabic word averages about five characters, so 300
# characters -- the schema's hard cap on the clarification prompt -- is about 60
# words, and at four tokens a word that is about 240 tokens. `reasoning` is one
# or two sentences the schema does not cap at all; allow the same again.
_ARABIC_TOKENS_PER_WORD = 4
_ENGLISH_TOKENS_PER_WORD = 1.3
_CHARS_PER_WORD = 5
_STRUCTURE_TOKENS = 40  # four keys plus the fixed ASCII component names
_PROMPT_CAP_CHARS = 300  # ClarificationPrompt's max_length


def _worst_case(tokens_per_word: float) -> float:
    """Structure, plus a capped clarification prompt, plus reasoning of the same
    length again -- the longest answer vague detection can return."""
    words = _PROMPT_CAP_CHARS / _CHARS_PER_WORD
    return _STRUCTURE_TOKENS + 2 * words * tokens_per_word


def test_the_vague_ceiling_fits_the_longest_arabic_answer():
    assert VAGUE_MAX_OUTPUT_TOKENS >= _worst_case(_ARABIC_TOKENS_PER_WORD)


def test_an_english_sized_ceiling_is_the_mistake_being_guarded_against():
    # The same answer in English is roughly a third of the tokens. A ceiling
    # sized on THAT would fit every English note and truncate ordinary Arabic
    # ones -- and truncation is malformed here, so it would not degrade an
    # Arabic note gracefully: it would spend the note's one reprompt and then
    # 503 it, for most of the corpus.
    english_sized = _worst_case(_ENGLISH_TOKENS_PER_WORD)
    assert english_sized < _worst_case(_ARABIC_TOKENS_PER_WORD)
    assert VAGUE_MAX_OUTPUT_TOKENS > english_sized


def test_the_pass_that_answers_in_the_notes_language_has_the_largest_ceiling():
    # Vague detection is the only one of the three whose answer carries free
    # text back in the note's own language. Classification returns one value
    # from a fixed ASCII vocabulary and scoring returns fixed ASCII keys and
    # whole numbers -- neither moves with Arabic, so neither needs the room.
    assert VAGUE_MAX_OUTPUT_TOKENS > SCORE_MAX_OUTPUT_TOKENS
    assert VAGUE_MAX_OUTPUT_TOKENS > CLASSIFY_MAX_OUTPUT_TOKENS
