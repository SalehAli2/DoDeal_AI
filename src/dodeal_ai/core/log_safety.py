"""Safe fields for logging an exception — type always, message only when the
message is one of ours.

Why this exists: a log line is a place raw content escapes to. `%r` or `str()`
on an exception prints whatever that exception chose to put in its message, and
for an exception raised by a library that is frequently the DATA — pydantic puts
the rejected `input_value` in its message, a KeyError's message is the missing
key, an httpx error can carry a URL with a query string. Note text and model
output must never be logged (ASSUMPTIONS §3.3), so a foreign exception is logged
by TYPE alone.

Our own exceptions are different: every exception class under `dodeal_ai`
constructs its message from a fixed vocabulary (a reason code, a short label, a
template filename) and never interpolates caller content — several say so in
their own docstrings. Those messages are safe and useful, so they are kept.

Anything added to `dodeal_ai` that raises with an interpolated value breaks that
promise; keep new exception messages fixed, and put the variable part in a
dedicated field instead.
"""

from __future__ import annotations

import traceback

# Classes defined under this package construct fixed-vocabulary messages.
_OUR_MODULE_PREFIX = "dodeal_ai."


def safe_error_fields(exc: BaseException) -> dict[str, str]:
    """Structured, content-free fields describing `exc`, for `extra=`.

    Always: `error_type` and `error_module`. `error` (the message) ONLY when the
    exception's class is defined under `dodeal_ai` — see the module docstring.
    `cause_type` names the chained exception's CLASS when there is one; its
    message is never included, because the cause is usually the foreign error.
    """
    exc_class = type(exc)
    fields = {
        "error_type": exc_class.__name__,
        "error_module": exc_class.__module__,
    }
    if exc_class.__module__.startswith(_OUR_MODULE_PREFIX):
        fields["error"] = str(exc)

    cause = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    if cause is not None:
        fields["cause_type"] = type(cause).__name__

    return fields


def frames_only(exc: BaseException) -> str:
    """The traceback FRAMES of `exc` — file, line, function, source line — with
    no exception line and no chained "During handling..." section.

    `traceback.format_exception` would append `str(exc)` and walk
    `__cause__`/`__context__`, printing every message in the chain. The frames
    alone are what makes the failure debuggable; the messages are the risk.
    """
    return "".join(traceback.format_tb(exc.__traceback__))


def chained(exc: BaseException) -> BaseException | None:
    """The exception ONE level down: `__cause__`, else `__context__`, else None.
    Never walks further, so a line carries at most two frames sections."""
    return exc.__cause__ if exc.__cause__ is not None else exc.__context__


def cause_frames_only(exc: BaseException) -> str | None:
    """The frames of `exc`'s chained exception, one level down, or None
    (register item 126). Frames only: the cause's message is never read."""
    cause = chained(exc)
    return None if cause is None else frames_only(cause)
