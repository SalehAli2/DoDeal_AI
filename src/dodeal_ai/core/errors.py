"""Central error handling — the fail-closed catch-all for UNEXPECTED errors.

This does NOT touch the deny paths. 401/403/429 are raised as HTTPException by
the gates and handled by FastAPI's own machinery; they are deliberate, shaped
responses and must pass through untouched. What we catch here is the
UNEXPECTED: an unhandled bug, a dependency (Redis, later the LLM) blowing up
mid-request. On those we fail closed — generic body, no stack trace, no internal
detail — and log the truth internally at ERROR with the request_id.

Why generic outward: an exception message can carry a file path, a query, a
secret fragment. The client gets none of it. The audit/error log gets enough to
debug (request_id + the real error, server-side only).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_logger = logging.getLogger("dodeal_ai.error")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for anything not already an HTTPException. Fail closed: the
    client sees a generic 500 with only the request_id (so they can quote it in
    a support request); the real error is logged server-side at ERROR."""
    request_id = _request_id(request)
    # exc_info=exc records the full traceback in the SERVER log only.
    _logger.error(
        "unhandled_exception request_id=%s error=%r",
        request_id,
        exc,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "request_id": request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Attach the catch-all. We explicitly re-register the HTTPException handler
    as a pass-through to FastAPI's default so our broad Exception handler cannot
    accidentally shadow the deny paths (401/403/429)."""

    # Keep deliberate HTTP errors exactly as they are — shaped, not swallowed.
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_passthrough(request: Request, exc: StarletteHTTPException):
        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)

    # Everything else -> generic, fail-closed 500.
    # Register against BOTH Exception and the 500 status code: Starlette routes
    # the broad server-error path through the status-code handler reliably,
    # whereas add_exception_handler(Exception, ...) alone can be bypassed by the
    # outer ServerErrorMiddleware.
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(500, unhandled_exception_handler)