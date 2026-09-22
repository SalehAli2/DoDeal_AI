"""copied_previous (register item 107): a note that repeats the one before it on
the lead is flagged -- from the next-older note on page one on the fetch route,
from the CRM's fingerprint on the direct route -- and its score is untouched."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.main import app
from dodeal_ai.tools.leads import get_leads_client
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.state import note_fingerprint
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import score_payload

LEAD_ID = 1656
TEXT = "Called the client about the 3BR, discussed price, following up Tuesday."


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def crm() -> FakeLeadsClient:
    return FakeLeadsClient(leads={LEAD_ID: lead(LEAD_ID)}, notes={LEAD_ID: []})


@pytest.fixture
def client(monkeypatch, llm, crm):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_leads_client] = lambda: crm
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _script(llm: FakeLLM, judgements: int = 1) -> None:
    llm.script_for(
        CLASSIFY_TEMPLATE, *[json_response({"note_type": "discovery"})] * judgements
    )
    llm.script_for(
        template_for(NoteType.DISCOVERY),
        *[
            json_response(
                {
                    "is_vague": False,
                    "missing_components": [],
                    "clarification_prompt": None,
                    "reasoning": "Complete.",
                }
            )
        ]
        * judgements,
    )
    llm.script_for(SCORE_TEMPLATE, *[json_response(score_payload())] * judgements)


def _fetch(client: TestClient, note_id: int = 10) -> dict:
    headers = {
        "Authorization": f"Bearer {tokens.mint_token(subdomain='tenant-a', sub=42)}",
        "Host": "tenant-a.dodealcrm.com",
    }
    body = {"lead_id": LEAD_ID, "note_id": note_id}
    return client.post("/api/v1/notes/judgements", json=body, headers=headers).json()


def _direct(client: TestClient, **extra: object):
    body = {
        "lead_id": LEAD_ID,
        "note_id": 10,
        "author_id": 27,
        "note_text": TEXT,
        "lead": {
            "leadType": None,
            "enquiryType": None,
            "project": None,
            "status": None,
        },
        **extra,
    }
    headers = {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }
    return client.post("/api/v1/notes/judgements/direct", json=body, headers=headers)


# --- the fetch route: the next-older note on page one -----------------------


def test_a_note_equal_after_strip_to_the_one_before_is_flagged(
    client, llm, crm
) -> None:
    """Newest first: the note after the target in the page is the older one."""
    crm.notes[LEAD_ID] = [note(10, TEXT), note(9, f"  {TEXT}\n")]
    _script(llm)
    assert _fetch(client)["analysis"]["copied_previous"] is True


def test_a_different_older_note_is_not_a_copy(client, llm, crm) -> None:
    crm.notes[LEAD_ID] = [note(10, TEXT), note(9, "Viewing booked for Sunday at 4.")]
    _script(llm)
    assert _fetch(client)["analysis"]["copied_previous"] is False


def test_the_oldest_note_on_the_page_has_nothing_before_it(client, llm, crm) -> None:
    crm.notes[LEAD_ID] = [note(11, TEXT), note(10, TEXT)]
    _script(llm)
    assert _fetch(client, note_id=10)["analysis"]["copied_previous"] is False


def test_a_copy_is_a_flag_and_changes_no_score(client, llm, crm) -> None:
    """The same answers score the same, copied or not."""
    _script(llm, judgements=2)
    crm.notes[LEAD_ID] = [note(10, TEXT), note(9, TEXT)]
    copied = _fetch(client)
    crm.notes[LEAD_ID] = [note(12, TEXT), note(11, "Something else entirely here.")]
    fresh = _fetch(client, note_id=12)
    assert copied["analysis"]["copied_previous"] is True
    assert copied["score"] == fresh["score"]
    assert copied["decision"]["action"] == fresh["decision"]["action"]


# --- the direct route: the CRM's fingerprint ----------------------------------


@pytest.mark.parametrize(
    ("fingerprint", "expected"),
    [
        (note_fingerprint(TEXT), True),
        (note_fingerprint(TEXT).upper(), True),
        (note_fingerprint("a different note"), False),
    ],
)
def test_the_previous_fingerprint_is_compared(
    client, llm, fingerprint, expected
) -> None:
    _script(llm)
    body = _direct(client, previous_note_fingerprint=fingerprint).json()
    assert body["analysis"]["copied_previous"] is expected


def test_no_previous_fingerprint_is_not_a_copy(client, llm) -> None:
    _script(llm)
    assert _direct(client).json()["analysis"]["copied_previous"] is False


@pytest.mark.parametrize("bad", ["abc", "g" * 64, "a" * 65])
def test_a_fingerprint_that_is_not_64_hex_chars_is_422(client, llm, bad) -> None:
    r = _direct(client, previous_note_fingerprint=bad)
    assert r.status_code == 422
    assert llm.call_count == 0
