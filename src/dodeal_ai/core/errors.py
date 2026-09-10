"""Central error handling — the fail-closed catch-all for UNEXPECTED errors.

This does NOT touch the deny paths. 401/403/429 are raised as HTTPException by
the gates and handled by FastAPI's own machinery; they are deliberate, shaped
responses and must pass through untouched. What we catch here is the
UNEXPECTED: an unhandled bug, a dependency (Redis, later the LLM) blowing up
mid-request. On those we fail closed — generic body, no stack trace, no internal
detail — and log the truth internally at ERROR with the request_id.

Why generic outward: an exception message can carry a file path, a query, a
secret fragment. The client gets none of it. The audit/error log gets enough to
debug: request_id, the exception TYPE, and the traceback frames — never the
exception message of a foreign error, which is where note text and model output
would surface. See core/log_safety.py.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from dodeal_ai.core.log_safety import frames_only, safe_error_fields

_logger = logging.getLogger("dodeal_ai.error")


class DodealError(Exception):
    """A DELIBERATE, enumerated failure a feature unit raises, carrying the
    status it becomes and the reason code the caller is told.

    This settles the error-taxonomy debt in STATUS §2 for UNIT errors only.
    The gates keep their hand-mapped HTTPExceptions and their existing bodies
    unchanged -- 401 {"detail": "Unauthorized"}, 403 {"detail": "Forbidden"},
    429 {"detail": "Too Many Requests"} -- because those bodies are a contract
    the CRM may already depend on, and test_chain.py pins the 429. Do not
    migrate the gates onto this class without changing that contract on
    purpose.

    RULE, as for every exception under dodeal_ai: `reason_code` comes from a
    FIXED vocabulary and is never interpolated with caller content. str() is
    the reason code alone, so this exception is safe on a log line
    (core/log_safety.py keeps the message of our own exceptions).
    """

    def __init__(self, reason_code: str, http_status: int) -> None:
        self.reason_code = reason_code
        self.http_status = http_status
        super().__init__(reason_code)

    def __str__(self) -> str:
        return self.reason_code


# --- the campaign's enumerated codes ---------------------------------------
# One subclass per code so a raise site names the failure rather than passing
# a string and a number, and so a typo is an ImportError instead of a new,
# undocumented code appearing in a response.


class InvalidRequestError(DodealError):
    def __init__(self) -> None:
        super().__init__("invalid_request", 422)


class LeadNotFoundError(DodealError):
    def __init__(self) -> None:
        super().__init__("lead_not_found", 404)


class NoteNotFoundError(DodealError):
    def __init__(self) -> None:
        super().__init__("note_not_found", 404)


class DuplicateRequestError(DodealError):
    def __init__(self) -> None:
        super().__init__("duplicate_request", 409)


class IdempotencyUnavailableResponse(DodealError):
    def __init__(self) -> None:
        super().__init__("idempotency_unavailable", 503)


class BackendUnavailableError(DodealError):
    def __init__(self) -> None:
        super().__init__("backend_unavailable", 503)


class ModelUnavailableError(DodealError):
    def __init__(self) -> None:
        super().__init__("model_unavailable", 503)


class MalformedOutputError(DodealError):
    def __init__(self) -> None:
        super().__init__("malformed_output", 503)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for anything not already an HTTPException. Fail closed: the
    client sees a generic 500 with only the request_id (so they can quote it in
    a support request); the real error is logged server-side at ERROR."""
    request_id = _request_id(request)
    # Structured fields only, and no exc_info: %r/exc_info would print the
    # exception's message and every chained message with it, which for a
    # foreign exception is the data that failed (see core/log_safety.py).
    # The traceback frames stay — they are what makes this debuggable.
    _logger.error(
        "unhandled_exception",
        extra={
            "request_id": request_id,
            **safe_error_fields(exc),
            "traceback": frames_only(exc),
        },
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "request_id": request_id},
    )


def _unit_error_body(reason_code: str, http_status: int, request_id: str) -> dict:
    """The one body shape every enumerated unit error returns.

    `detail` is the STATUS PHRASE, not a message: it is derived from the status
    code and so cannot accidentally carry a note body, a model completion or a
    backend error string. `reason` is the machine-readable code the CRM
    branches on. `request_id` is what a support ticket quotes.
    """
    return {
        "detail": HTTPStatus(http_status).phrase,
        "reason": reason_code,
        "request_id": request_id,
    }


async def dodeal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a DodealError as its enumerated status and reason code.

    Logged at WARNING, not ERROR: these are deliberate, expected outcomes (a
    duplicate request, a store that is down) and an ERROR line per 409 would
    drown the ones that mean a bug. Fields only -- no traceback, because
    nothing here is unexplained.
    """
    assert isinstance(exc, DodealError)  # registered for this type only
    request_id = _request_id(request)
    _logger.warning(
        "unit_error",
        extra={
            "request_id": request_id,
            "reason_code": exc.reason_code,
            "http_status": exc.http_status,
        },
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=_unit_error_body(exc.reason_code, exc.http_status, request_id),
    )


async def request_validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """A malformed body becomes 422 invalid_request in OUR shape.

    FastAPI's stock handler returns jsonable_encoder(exc.errors()), and a
    pydantic error carries `input` -- THE CALLER'S OWN SUBMITTED VALUE. For
    this service that value is a CRM payload, so the stock handler would echo
    caller content straight back out and into whatever logs the response.
    Every field name, location and message is dropped here; the caller gets the
    code, and the field-level detail stays server-side.

    JudgementRequest is extra="forbid", so posting note text is a 422 rather
    than a silently ignored field ON THE PRIMARY ROUTE -- and this is why that
    422 does not then quote the note back. The direct route
    (DECISION[DIRECT_ROUTE]) accepts note text by design and caps it in the
    schema, so its over-length 422 comes through here carrying a whole note as
    the rejected `input`: the same drop, on the one path where the dropped
    value is certain to be a note body.
    """
    assert isinstance(exc, RequestValidationError)  # registered for this type only
    request_id = _request_id(request)
    # Count and error TYPES only -- pydantic's own fixed vocabulary. Never the
    # locations (a field name can be attacker-chosen) and never the values.
    errors = exc.errors()
    _logger.warning(
        "request_validation_failed",
        extra={
            "request_id": request_id,
            "reason_code": "invalid_request",
            "error_count": len(errors),
            "error_types": ",".join(sorted({item["type"] for item in errors})),
        },
    )
    return JSONResponse(
        status_code=422,
        content=_unit_error_body("invalid_request", 422, request_id),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Attach the catch-all. We explicitly re-register the HTTPException handler
    as a pass-through to FastAPI's default so our broad Exception handler cannot
    accidentally shadow the deny paths (401/403/429)."""

    # Enumerated unit failures -> their own status and reason code. Registered
    # BEFORE the broad Exception handler; Starlette walks the exception's MRO
    # and takes the most specific registration, so a DodealError never falls
    # through to the generic 500.
    app.add_exception_handler(DodealError, dodeal_error_handler)

    # A malformed body -> 422 invalid_request in our shape, replacing FastAPI's
    # stock handler, which echoes the caller's own submitted value back.
    app.add_exception_handler(RequestValidationError, request_validation_handler)

    # Keep deliberate HTTP errors exactly as they are — shaped, not swallowed.
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_passthrough(
        request: Request, exc: StarletteHTTPException
    ):
        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)

    # Everything else -> generic, fail-closed 500.
    # Register against BOTH Exception and the 500 status code: Starlette routes
    # the broad server-error path through the status-code handler reliably,
    # whereas add_exception_handler(Exception, ...) alone can be bypassed by the
    # outer ServerErrorMiddleware.
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(500, unhandled_exception_handler)
