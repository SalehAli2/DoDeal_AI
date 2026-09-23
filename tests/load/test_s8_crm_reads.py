"""Load scenario 8 (register item 74): CRM reads under concurrency.

Fifteen fetch-route judgements at once through the real LeadsClient, over
httpx.ASGITransport, against the in-process fake backend
(tests/integration/fake_backend.py) serving the vendored corpus. Every
judgement reads the CRM exactly twice -- the lead and its notes -- except a
note the backend hides on its first read, which is read again once; a note that
never appears is read again once and then 404. Nothing reads a third time.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import httpx
import pytest
from pydantic import SecretStr

from dodeal_ai.core.config import get_settings
from dodeal_ai.main import app
from dodeal_ai.tools.httpx_transport import HttpxTransport
from dodeal_ai.tools.keys import SettingsKeyResolver
from dodeal_ai.tools.leads import LeadsClient, get_leads_client
from tests.integration.fake_backend import EXPECTED_API_KEY, FakeBackend
from tests.load.conftest import TENANT

BURST = 15


def _backend(lane, picked, hidden_once: set[int]) -> FakeBackend:
    """The corpus leads and notes the burst reads, as the CRM would serve them."""
    corpus = lane.leads
    return FakeBackend(
        leads={
            lead_id: corpus.leads[lead_id].model_dump(mode="json")
            for lead_id, _ in picked
        },
        notes={
            lead_id: [note.model_dump(mode="json") for note in corpus.notes[lead_id]]
            for lead_id, _ in picked
        },
        hidden_once=hidden_once,
    )


def _wire(backend: FakeBackend) -> None:
    settings = get_settings().model_copy(
        update={"dd_api_keys": {TENANT: SecretStr(EXPECTED_API_KEY)}}
    )
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=backend.app))
    client = LeadsClient(HttpxTransport(http), SettingsKeyResolver(settings), settings)
    app.dependency_overrides[get_leads_client] = lambda: client


@pytest.fixture
def picked(lane):
    """One note per lead, distinct texts, so no judgement is another's duplicate."""
    distinct = {note.note: (lead_id, note) for lead_id, note in lane.notes}
    chosen = list(distinct.values())[:BURST]
    assert len(chosen) == BURST
    return chosen


async def test_each_judgement_reads_twice_and_a_late_note_once_more(
    lane, picked
) -> None:
    """Two reads a judgement; the note hidden on its first read costs one more."""
    late_lead, late_note = picked[0]
    backend = _backend(lane, picked, hidden_once={late_note.id})
    _wire(backend)
    lane.script(BURST)

    responses = await asyncio.gather(
        *(lane.judge(lead_id, note.id) for lead_id, note in picked)
    )

    assert [response.status_code for response in responses] == [200] * BURST
    reads = Counter(backend.paths)
    for lead_id, _ in picked:
        assert reads[f"/api/service/leads/{lead_id}"] == 1
        expected_notes = 2 if lead_id == late_lead else 1
        assert reads[f"/api/service/leads/{lead_id}/notes"] == expected_notes
    assert len(backend.paths) == 2 * BURST + 1


async def test_a_note_that_never_appears_is_read_again_once_and_is_404(
    lane, picked
) -> None:
    """At most one re-read: three requests, then note_not_found."""
    lead_id, _ = picked[0]
    backend = _backend(lane, picked[:1], hidden_once=set())
    _wire(backend)

    response = await lane.judge(lead_id, 999_999)

    assert response.status_code == 404
    assert response.json()["reason"] == "note_not_found"
    assert Counter(backend.paths) == {
        f"/api/service/leads/{lead_id}": 1,
        f"/api/service/leads/{lead_id}/notes": 2,
    }
