"""Logging configuration -- configures Python logging ONCE, at startup, so
every dodeal_ai.* log line (audit, error, cost, resilience, validation, ...)
is actually written, as one structured JSON line to stdout.

Without this, nothing configures logging anywhere in the process: the root
logger defaults to WARNING with no handler, so INFO-level audit "allow"
lines are silently dropped before they reach any handler, and WARNING/ERROR
lines only escape via Python's undocumented `lastResort` stderr fallback --
not a real, parseable stream a log collector can index. See ASSUMPTIONS.md.

Called from main.py's lifespan, before the app starts serving.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.log_safety import cause_frames_only, chained, frames_only

# Every attribute a stock LogRecord carries. Anything ELSE on the record --
# set via `extra=` on a logging call, or by a `logging.Filter` (e.g. a
# future request-id filter) -- is a caller-supplied structured field and
# gets merged into the JSON line at the top level.
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime"}


# uvicorn's own loggers. "uvicorn" is the parent, but uvicorn configures all
# three explicitly, so all three need handing back -- clearing only the parent
# leaves uvicorn.error and uvicorn.access still holding their own handlers.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


# The job a worker line belongs to (register item 54): set around each call
# task by `job_log_context`, read by JobContextFilter, None outside a task.
_JOB_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar(
    "dodeal_job_context", default=None
)


@contextmanager
def job_log_context(*, tenant: str, job_id: str) -> Iterator[dict[str, str]]:
    """Every line logged inside carries this tenant, job_id and request_id.
    The request_id is "unknown" until the task has read its job and set it on
    the dict this yields."""
    fields = {"tenant": tenant, "job_id": job_id, "request_id": "unknown"}
    token = _JOB_CONTEXT.set(fields)
    try:
        yield fields
    finally:
        _JOB_CONTEXT.reset(token)


class JobContextFilter(logging.Filter):
    """Adds the current job's three ids to a record that does not already
    carry them. A filter, not a record factory: a factory's attribute would
    make every `extra=` naming the same field raise."""

    def filter(self, record: logging.LogRecord) -> bool:
        fields = _JOB_CONTEXT.get()
        if fields is not None:
            for name, value in fields.items():
                if not hasattr(record, name):
                    setattr(record, name, value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Structured fields reach the output ONE way: extra=/Filter-set attributes
    on the record, merged at the TOP level so nothing is ever a JSON string
    nested inside another JSON string. The audit logger uses exactly that.

    The message is NEVER parsed. An earlier version unpacked a message that
    was itself a JSON object, which meant any log line whose text began with
    "{" -- a note body, a model completion, an echoed payload -- could set or
    overwrite top-level fields such as decision or reason_code and forge an
    audit record. The message is always a plain string under "message".

    An exception is NEVER formatted either, for the same reason. A record
    logged with exc_info yields "exc_type" (the class, module-qualified) and
    "exc_frames" (the traceback frames); THE "exc_info" KEY IS GONE, so a
    collector query on it now matches nothing and has to move to those two.
    `self.formatException` printed str(exc) and every message in the
    __cause__/__context__ chain, and for an exception raised OUTSIDE this
    package -- by starlette's ServerErrorMiddleware, by uvicorn, by any
    library logging through our root handler -- that message is frequently
    the data that failed: a note body, a model completion, a backend error
    string. The frames stay, because they are what makes a failure
    debuggable; the messages are the risk, and no logger in the process can
    put one on stdout. See core/log_safety.py.

    record.stack_info is ignored, before this change and after: it is set
    only by an explicit stack_info=True, which nothing here passes.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {"message": record.getMessage()}

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value

        # record.exc_text is deliberately never read: the stdlib caches the
        # MESSAGE-BEARING formatted exception there when a record is formatted
        # twice, which is the text this branch exists to keep off stdout.
        exc_info = record.exc_info
        if isinstance(exc_info, tuple):
            # Tuple shape only. A bare exc_info=True is resolved to a tuple by
            # the logging module before a formatter sees it; anything else on
            # this attribute has no frames to read, and a formatter never raises.
            exc_class, exc_value = exc_info[0], exc_info[1]
            if exc_class is not None:
                # The two halves safe_error_fields reports separately
                # (error_type, error_module), joined. Never str(exc).
                payload["exc_type"] = f"{exc_class.__module__}.{exc_class.__name__}"
            if exc_value is not None:
                payload["exc_frames"] = frames_only(exc_value)
                # One level of chaining (register item 126): where the failure
                # came FROM, as a class and frames, never as a message.
                cause = chained(exc_value)
                if cause is not None:
                    cause_class = type(cause)
                    payload["exc_cause_type"] = (
                        f"{cause_class.__module__}.{cause_class.__name__}"
                    )
                    payload["exc_cause_frames"] = cause_frames_only(exc_value)

        # Applied last so nothing above can shadow these.
        payload["timestamp"] = datetime.fromtimestamp(
            record.created, tz=UTC
        ).isoformat()
        payload["level"] = record.levelname
        payload["logger"] = record.name

        return json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)


def warn_if_demo_audio(settings: Settings) -> None:
    """One ERROR at startup while CALL_DEMO_ALLOW_LOCAL_AUDIO is on: the
    service will fetch from and post to http and loopback. Not a refusal --
    the demo needs it -- but never quiet (register item "demo")."""
    if settings.call_demo_allow_local_audio:
        logging.getLogger("dodeal_ai.startup").error(
            "call_demo_audio_insecure -- call audio and callbacks may use http "
            "and loopback. Demo only; anywhere real a push can aim this "
            "service at its own network.",
            extra={"event": "call_demo_audio_insecure"},
        )


def configure_logging() -> None:
    """Configure logging once. Idempotent -- safe to call more than once
    (replaces, never accumulates, handlers).

    - stdout only: containers collect stdout, so no log files are written.
    - The "dodeal_ai" logger tree (audit, error, cost, ...) is set to the
      configured level (DODEAL_LOG_LEVEL, default INFO) so audit ALLOW
      lines are not dropped.
    - The root logger stays at WARNING, which is what third-party libraries
      (with no "dodeal_ai" ancestor) inherit -- so this does not flood logs
      with dependency chatter.
    - uvicorn's three loggers are stripped of the handlers uvicorn installs
      for itself and made to propagate, so its access/error lines reach the
      SAME JSON handler as ours instead of being written as plain text. See
      _UVICORN_LOGGERS.
    """
    settings = get_settings()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    # Register item 54: a worker line names its job wherever it was logged.
    handler.addFilter(JobContextFilter())

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers = [handler]

    logging.getLogger("dodeal_ai").setLevel(settings.log_level)

    # uvicorn installs its own handlers on these three and sets
    # propagate=False, so its lines bypass the JSON handler above and land on
    # stdout as plain text -- a log collector then sees two formats in one
    # stream and cannot index the request lines. Handing them back to the root
    # handler makes every line in the process one JSON object. Levels are left
    # exactly as uvicorn set them: this changes the FORMAT, not what is logged.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
