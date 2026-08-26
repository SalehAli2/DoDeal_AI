"""validate_output's failure path: the rejected content stays out of the raised
error entirely — no pydantic ValidationError stored, none chained, no input.

Audit finding H1: OutputValidationError used to hold the ValidationError, whose
errors() and str() carry `input_value` — the rejected note body or model output.
Anything that formatted the traceback then printed it.
"""

from __future__ import annotations

import pytest

from dodeal_ai.core.validation import OutputValidationError, validate_output
from dodeal_ai.schemas.lead import LeadNotesResponse

SENTINEL = "SENTINEL-0501234567 villa budget 4.2M"


def _notes_payload_with_bad_note() -> dict:
    """Valid in every field except `note`, which is a list where a string is
    required — so validation reports exactly one error, at data.0.note."""
    return {
        "status": True,
        "data": [
            {
                "id": 1,
                "note": [SENTINEL],
                "author": "Jane Doe",
                "author_id": 10,
                "createdAt": "2026-01-01T10:00:00+00:00",
            }
        ],
        "meta": {"current_page": 1, "per_page": 15, "total": 1, "last_page": 1},
    }


@pytest.fixture
def failure() -> OutputValidationError:
    with pytest.raises(OutputValidationError) as caught:
        validate_output(
            LeadNotesResponse,
            _notes_payload_with_bad_note(),
            label="tool.get_lead_notes",
        )
    return caught.value


def test_errors_are_location_and_type_pairs_only(failure):
    # Both halves are fixed vocabulary: a dotted field path we control and
    # pydantic's own error type. Neither can contain caller content.
    assert failure.errors == (("data.0.note", "string_type"),)
    assert failure.error_count == 1
    assert failure.label == "tool.get_lead_notes"
    assert str(failure) == "output failed validation: tool.get_lead_notes"


def test_pydantic_error_is_not_chained(failure):
    # `raise ... from None`: nothing that formats this traceback can reach the
    # ValidationError and print its input_value. Deliberate — see validation.py.
    assert failure.__cause__ is None
    assert failure.__suppress_context__ is True


def test_no_attribute_holds_the_rejected_input(failure):
    assert all(SENTINEL not in repr(value) for value in vars(failure).values())
    assert SENTINEL not in repr(failure.args)
    assert SENTINEL not in repr(failure)
    assert SENTINEL not in str(failure)
