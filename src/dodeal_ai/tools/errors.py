"""Typed backend failures (register item 89).

A Transport raises BackendStatusError for a non-2xx reply; LeadsClient turns the
ones that will not change on a retry into the four typed errors below. Every
message is a fixed code: no body, no header value, no URL.
"""

from __future__ import annotations

import math
from typing import ClassVar


class BackendStatusError(Exception):
    """The CRM answered with a status outside 2xx. `retry_after` is the
    Retry-After header in seconds when it was a usable number, else None."""

    def __init__(self, status: int, *, retry_after: float | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"backend_status:{status}")


class BackendError(Exception):
    """A 4xx the pipeline branches on. `label` names the read that failed."""

    reason_code: ClassVar[str] = "backend_error"

    def __init__(self, label: str, status: int) -> None:
        self.label = label
        self.status = status
        super().__init__(self.reason_code)

    def __str__(self) -> str:
        return self.reason_code


class BackendUnauthorized(BackendError):
    """401: the tenant's DD-API-KEY was refused."""

    reason_code = "backend_unauthorized"


class BackendForbidden(BackendError):
    """403: the key is valid but not for this resource."""

    reason_code = "backend_forbidden"


class BackendNotFound(BackendError):
    """404: the resource the read named does not exist."""

    reason_code = "backend_not_found"


class BackendRejected(BackendError):
    """Any other 4xx except 429: the CRM refused the request as sent."""

    reason_code = "backend_rejected"


class BackendEnvelopeInvalid(Exception):
    """A 2xx whose JSON is not the response shape the CRM documents (register
    item F1). `errors` holds (field path, pydantic error type) pairs, never a
    value; str() is the fixed code."""

    reason_code: ClassVar[str] = "backend_envelope_invalid"

    def __init__(self, label: str, errors: tuple[tuple[str, str], ...]) -> None:
        self.label = label
        self.errors = errors
        super().__init__(self.reason_code)

    def __str__(self) -> str:
        return self.reason_code


def retry_after_seconds(value: str | None) -> float | None:
    """Retry-After in its delta-seconds form. The HTTP-date form, a negative
    number and junk all read as absent, so the caller falls back to jitter."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
