"""DodealError: every campaign code maps to its status, str() stays a fixed
reason code, and the body shape is the same for all of them."""

from __future__ import annotations

import json
from http import HTTPStatus

import pytest

from dodeal_ai.core.errors import (
    BackendUnavailableError,
    DodealError,
    DuplicateRequestError,
    IdempotencyUnavailableResponse,
    InvalidRequestError,
    JudgementDeadlineExceeded,
    LeadNotFoundError,
    LoadShed,
    MalformedOutputError,
    ModelUnavailableError,
    NoteNotFoundError,
    _unit_error_body,
    dodeal_error_response,
)

# Every enumerated code the campaign defines, with the status it becomes.
_CODES = [
    (InvalidRequestError, "invalid_request", 422),
    (LeadNotFoundError, "lead_not_found", 404),
    (NoteNotFoundError, "note_not_found", 404),
    (DuplicateRequestError, "duplicate_request", 409),
    (IdempotencyUnavailableResponse, "idempotency_unavailable", 503),
    (BackendUnavailableError, "backend_unavailable", 503),
    (ModelUnavailableError, "model_unavailable", 503),
    (MalformedOutputError, "malformed_output", 503),
    # 503 and not 504: the model_unavailable family (register item 83).
    (JudgementDeadlineExceeded, "judgement_deadline_exceeded", 503),
    # Raised by nothing -- middleware/inflight.py cannot raise it (the handlers
    # live inside the middleware stack) and builds the response directly
    # instead. It is in the taxonomy so that the code, the status and the body
    # shape are the same ones every other refusal uses.
    (LoadShed, "load_shed", 503),
]


@pytest.mark.parametrize(("error_class", "reason_code", "status"), _CODES)
def test_each_code_maps_to_its_status(error_class, reason_code, status) -> None:
    exc = error_class()
    assert exc.reason_code == reason_code
    assert exc.http_status == status


@pytest.mark.parametrize(("error_class", "reason_code", "status"), _CODES)
def test_str_is_the_reason_code_alone(error_class, reason_code, status) -> None:
    # log_safety keeps the message of any exception defined under dodeal_ai, so
    # these must never interpolate note text, a model completion or a key.
    assert str(error_class()) == reason_code


@pytest.mark.parametrize(("error_class", "reason_code", "status"), _CODES)
def test_every_code_is_a_dodeal_error(error_class, reason_code, status) -> None:
    # Starlette walks the MRO, so this is what keeps them off the generic 500.
    assert isinstance(error_class(), DodealError)


def test_the_body_shape_is_the_same_for_every_code() -> None:
    body = _unit_error_body("duplicate_request", 409, "req-1")
    assert body == {
        "detail": "Conflict",
        "reason": "duplicate_request",
        "request_id": "req-1",
    }


def test_detail_is_the_status_phrase_not_a_message() -> None:
    # Derived from the status code, so it cannot carry a note body, a model
    # completion or a backend error string.
    for _, reason_code, status in _CODES:
        body = _unit_error_body(reason_code, status, "req-1")
        assert body["detail"] == HTTPStatus(status).phrase


def test_reason_codes_are_unique() -> None:
    codes = [reason for _, reason, _ in _CODES]
    assert len(codes) == len(set(codes))


def test_the_base_class_takes_a_code_and_a_status() -> None:
    exc = DodealError("something_specific", 418)
    assert (exc.reason_code, exc.http_status) == ("something_specific", 418)


def test_a_middleware_gets_the_same_body_the_handler_would_build() -> None:
    """`dodeal_error_response` exists because middleware cannot raise.

    Starlette builds ExceptionMiddleware INSIDE the user middleware stack, so a
    DodealError raised in middleware never reaches its handler -- it reaches
    ServerErrorMiddleware and comes back as a generic 500. This renders the same
    body directly, and two spellings of the error body is how a CRM ends up
    branching on a `reason` that only some refusals carry.
    """
    response = dodeal_error_response(LoadShed(), "req-1")

    assert response.status_code == 503
    assert json.loads(response.body) == _unit_error_body("load_shed", 503, "req-1")
