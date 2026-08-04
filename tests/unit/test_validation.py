"""Response validation: good output parses, malformed is rejected and never surfaced."""
from __future__ import annotations

import pytest

from dodeal_ai.core.validation import OutputValidationError, validate_output
from schemas.lead_v1 import LeadV1


def test_valid_output_parses():
    raw = {"id": 1, "name": "Acme Corp", "status": "open"}
    lead = validate_output(LeadV1, raw, label="tool.get_lead")
    assert isinstance(lead, LeadV1)
    assert lead.id == 1
    assert lead.name == "Acme Corp"


def test_missing_field_rejected():
    raw = {"id": 1, "name": "Acme Corp"}  # no status
    with pytest.raises(OutputValidationError) as exc:
        validate_output(LeadV1, raw, label="tool.get_lead")
    assert exc.value.label == "tool.get_lead"


def test_wrong_type_rejected():
    raw = {"id": "not-an-int", "name": "Acme", "status": "open"}
    with pytest.raises(OutputValidationError):
        validate_output(LeadV1, raw, label="tool.get_lead")


def test_unexpected_extra_field_rejected():
    # extra="forbid" means a surprise field fails loudly rather than passing.
    raw = {"id": 1, "name": "Acme", "status": "open", "secret": "leak"}
    with pytest.raises(OutputValidationError):
        validate_output(LeadV1, raw, label="tool.get_lead")


def test_raw_content_not_in_exception_message():
    # The exception must not carry the raw invalid content in its string form.
    raw = {"id": 1, "name": "Acme", "status": "open", "sensitive": "xyz-123"}
    with pytest.raises(OutputValidationError) as exc:
        validate_output(LeadV1, raw, label="tool.get_lead")
    assert "xyz-123" not in str(exc.value)