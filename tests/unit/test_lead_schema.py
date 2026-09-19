"""Confirmed lead and note schemas: parses the data-wrapper response, reads
leads/notes from `data`, and validates the single-lead and notes shapes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from dodeal_ai.core.validation import OutputValidationError, validate_output
from dodeal_ai.schemas.lead import (
    LeadListResponse,
    LeadNote,
    LeadNotesResponse,
    LeadResponse,
)


def _lead_payload(**overrides) -> dict:
    base = {
        "id": 1,
        "name": "Acme Corp",
        "email": "info@acme.test",
        "status": "open",
        "bookedAmount": 1500.0,
    }
    base.update(overrides)
    return base


def _meta(total: int) -> dict:
    return {"current_page": 1, "per_page": 25, "total": total, "last_page": 1}


def _response_payload(leads: list[dict]) -> dict:
    return {"status": True, "data": leads, "meta": _meta(len(leads))}


def test_parses_response_and_reads_leads_from_data():
    payload = _response_payload([_lead_payload(id=1), _lead_payload(id=2)])
    result = validate_output(LeadListResponse, payload, label="tool.get_leads")
    assert isinstance(result, LeadListResponse)
    assert result.status is True
    assert len(result.data) == 2
    assert result.data[0].id == 1


def test_lead_tolerates_extra_backend_fields():
    # extra="ignore": a field we did not model must not break validation.
    payload = _response_payload([_lead_payload(id=1, unmodelled_field="x")])
    result = validate_output(LeadListResponse, payload, label="tool.get_leads")
    assert result.data[0].id == 1
    assert not hasattr(result.data[0], "unmodelled_field")


def test_optional_lead_fields_may_be_absent():
    minimal = {"id": 5}  # only id is required
    result = validate_output(
        LeadListResponse, _response_payload([minimal]), label="tool.get_leads"
    )
    assert result.data[0].id == 5
    assert result.data[0].name is None


def test_lead_tolerates_null_booked_amount():
    # bookedAmount's real type is unconfirmed; the backend may send null.
    payload = _response_payload([_lead_payload(id=1, bookedAmount=None)])
    result = validate_output(LeadListResponse, payload, label="tool.get_leads")
    assert result.data[0].bookedAmount is None


def test_lead_accepts_any_booked_amount():
    """Register item 90: bookedAmount's type is unconfirmed, so any value loads."""
    for value in ("1,250,000", 1500, {"amount": 1}):
        payload = _response_payload([_lead_payload(id=1, bookedAmount=value)])
        result = validate_output(LeadListResponse, payload, label="tool.get_leads")
        assert result.data[0].bookedAmount == value


def test_lead_requires_id():
    bad = {"name": "No ID"}  # missing id
    with pytest.raises(OutputValidationError):
        validate_output(
            LeadListResponse, _response_payload([bad]), label="tool.get_leads"
        )


def test_wrong_wrapper_shape_rejected():
    # Old assumption {status, posts: {data}} must NOT validate anymore.
    old_shape = {"status": True, "posts": {"data": []}}
    with pytest.raises(OutputValidationError):
        validate_output(LeadListResponse, old_shape, label="tool.get_leads")


def test_single_lead_response_parses():
    payload = {"status": True, "data": _lead_payload(id=7)}
    result = validate_output(LeadResponse, payload, label="tool.get_lead")
    assert result.data.id == 7


def test_notes_response_parses_and_tolerates_null_author():
    payload = {
        "status": True,
        "data": [
            {
                "id": 1,
                "note": "Called, no answer.",
                "author": "Jane Doe",
                "author_id": 10,
                "createdAt": "2026-01-01T10:00:00+00:00",
            },
            {
                "id": 2,
                "note": "Left voicemail.",
                "author": None,
                "author_id": 11,
                "createdAt": "2026-01-02T10:00:00+00:00",
            },
        ],
        "meta": _meta(2),
    }
    result = validate_output(LeadNotesResponse, payload, label="tool.get_lead_notes")
    assert len(result.data) == 2
    assert result.data[0].author == "Jane Doe"
    assert result.data[1].author is None


def test_empty_notes_list_is_valid():
    # A lead with no notes returns data: [] with a 200, not an error.
    payload = {"status": True, "data": [], "meta": _meta(0)}
    result = validate_output(LeadNotesResponse, payload, label="tool.get_lead_notes")
    assert result.data == []


# --- LeadNote.createdAt is an aware datetime (register item 32) --------------


def _note(created_at: object) -> dict:
    return {"id": 1, "note": "Called.", "author_id": 10, "createdAt": created_at}


def test_an_offset_timestamp_keeps_its_offset():
    """An ISO-8601 time with an offset parses to that aware instant."""
    note = LeadNote.model_validate(_note("2026-01-01T10:00:00+04:00"))
    assert note.createdAt == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    assert note.createdAt.utcoffset() == timedelta(hours=4)


def test_a_naive_timestamp_is_read_as_utc():
    """ASSUMPTION[Q5]: no offset means UTC, never local time."""
    note = LeadNote.model_validate(_note("2026-01-01T10:00:00"))
    assert note.createdAt == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert note.createdAt.tzinfo is UTC


def test_a_z_timestamp_is_utc():
    """The Z suffix is an offset of zero."""
    note = LeadNote.model_validate(_note("2026-01-01T10:00:00Z"))
    assert note.createdAt.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", ["", "yesterday", None])
def test_an_unparseable_created_at_is_rejected(value):
    """The field stays required: empty, junk and null are not times."""
    with pytest.raises(ValidationError):
        LeadNote.model_validate(_note(value))


def test_every_parsed_created_at_is_aware():
    """No path yields a naive datetime a comparison could trip on."""
    for raw in ("2026-01-01T10:00:00", "2026-01-01T10:00:00-05:00", "2026-01-01"):
        assert LeadNote.model_validate(_note(raw)).createdAt.tzinfo is not None
