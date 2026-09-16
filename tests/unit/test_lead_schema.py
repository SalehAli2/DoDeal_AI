"""Confirmed lead and note schemas: parses the data-wrapper response, reads
leads/notes from `data`, and validates the single-lead and notes shapes."""

from __future__ import annotations

import pytest

from dodeal_ai.core.validation import OutputValidationError, validate_output
from dodeal_ai.schemas.lead import LeadListResponse, LeadNotesResponse, LeadResponse


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
