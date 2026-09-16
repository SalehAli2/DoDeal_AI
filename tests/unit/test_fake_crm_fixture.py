"""Tripwires on the vendored fake-CRM corpus.

`tests/fixtures/fake_crm/tenant-a.json` is a generated artefact, and the eval
suite reads meaning into its size: "the 127-note corpus" is a number the
campaign quotes, not a number a test discovers. So the seed and the counts are
pinned here. Regenerating the fixture with a different seed, or with more or
fewer records, fails THIS test with the old and new numbers side by side --
rather than quietly changing what an eval pass means somewhere downstream.

`skipped_invalid_leads` is pinned for the opposite reason. Since register item
90 made `bookedAmount` `Any`, every lead in the corpus validates; a rejected
lead would be a new disagreement between the corpus and the schemas, and the
point of pinning the number is that it cannot arrive silently.

The per-note assertion is the shape tripwire. Every note is loaded through
`LeadNote`, which requires a non-null `note`, `id`, `author_id` and
`createdAt`; a regenerated corpus that reintroduced a null-text note (review
finding F1) could not be loaded at all, and would fail here at load.
"""

from __future__ import annotations

from dodeal_ai.schemas.lead import LeadNote
from tests.helpers.fake_leads import load_fixture_client

# What the file at HEAD holds. Read off the fixture, not guessed: change these
# only together with the fixture, and say why in the same commit.
SEED = "fake-dodeal-crm-seed-1"
LEADS_IN_FILE = 1448
SKIPPED_INVALID_LEADS = 0  # lead 1661's "1,250,000" loads since item 90
LEAD_COUNT = LEADS_IN_FILE - SKIPPED_INVALID_LEADS
NOTE_COUNT = 127


def test_fixture_seed_is_pinned():
    client = load_fixture_client()
    assert client.seed == SEED


def test_fixture_lead_count_is_pinned():
    client = load_fixture_client()
    assert len(client.leads) == LEAD_COUNT


def test_skipped_invalid_lead_count_is_pinned():
    # Not "some leads are bad, never mind" -- none is, and the loader says so
    # out loud. One would fail here.
    client = load_fixture_client()
    assert client.skipped_invalid_leads == SKIPPED_INVALID_LEADS


def test_fixture_note_count_is_pinned():
    client = load_fixture_client()
    total = sum(len(notes) for notes in client.notes.values())
    assert total == NOTE_COUNT


def test_every_loaded_note_is_a_lead_note():
    client = load_fixture_client()
    loaded = [n for notes in client.notes.values() for n in notes]
    assert len(loaded) == NOTE_COUNT
    # isinstance alone would pass on an empty corpus; the count above is what
    # makes this an assertion about all 127.
    assert all(isinstance(n, LeadNote) for n in loaded)
