"""Confirmed lead schema: parses the wrapped response, reads leads from posts.data."""

from __future__ import annotations

import pytest

from dodeal_ai.core.validation import OutputValidationError, validate_output
from schemas.lead import LeadListResponse


def _lead_payload(**overrides) -> dict:
    base = {
        "id": 1,
        "leadName": "Acme Corp",
        "leadEmail": "info@acme.test",
        "leadStatus": "open",
        "booked_amount": 1500.0,
    }
    base.update(overrides)
    return base


def _response_payload(leads: list[dict]) -> dict:
    return {
        "status": True,
        "posts": {"current_page": 1, "total": len(leads), "data": leads},
    }


def test_parses_wrapped_response_and_reads_leads_from_posts_data():
    payload = _response_payload([_lead_payload(id=1), _lead_payload(id=2)])
    result = validate_output(LeadListResponse, payload, label="tool.get_leads")
    assert isinstance(result, LeadListResponse)
    assert result.status is True
    assert len(result.posts.data) == 2
    assert result.posts.data[0].id == 1


def test_lead_tolerates_extra_backend_fields():
    # extra="ignore": a field we did not model must not break validation.
    payload = _response_payload([_lead_payload(id=1, unmodelled_field="x")])
    result = validate_output(LeadListResponse, payload, label="tool.get_leads")
    assert result.posts.data[0].id == 1
    assert not hasattr(result.posts.data[0], "unmodelled_field")


def test_optional_lead_fields_may_be_absent():
    minimal = {"id": 5}  # only id is required
    result = validate_output(
        LeadListResponse, _response_payload([minimal]), label="tool.get_leads"
    )
    assert result.posts.data[0].id == 5
    assert result.posts.data[0].leadName is None


def test_lead_requires_id():
    bad = {"leadName": "No ID"}  # missing id
    with pytest.raises(OutputValidationError):
        validate_output(
            LeadListResponse, _response_payload([bad]), label="tool.get_leads"
        )


def test_wrong_wrapper_shape_rejected():
    # Old assumption {success, message, data, meta} must NOT validate.
    old_shape = {"success": True, "message": "ok", "data": [], "meta": {}}
    with pytest.raises(OutputValidationError):
        validate_output(LeadListResponse, old_shape, label="tool.get_leads")
