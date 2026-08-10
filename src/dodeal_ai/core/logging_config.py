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
from datetime import UTC, datetime

from dodeal_ai.core.config import get_settings

# Every attribute a stock LogRecord carries. Anything ELSE on the record --
# set via `extra=` on a logging call, or by a `logging.Filter` (e.g. a
# future request-id filter) -- is a caller-supplied structured field and
# gets merged into the JSON line at the top level.
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime"}


def _try_parse_json_object(text: str) -> dict[str, object] | None:
    if not text.startswith("{"):
        return None
    try:
        candidate = json.loads(text)
    except ValueError:
        return None
    return candidate if isinstance(candidate, dict) else None


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Two ways structured fields reach the output, both merged at the TOP
    level so nothing is ever a JSON string nested inside another JSON
    string:
      - extra=/Filter-set attributes on the record (the general mechanism).
      - a message that IS ALREADY a JSON object (the audit logger's
        existing pattern: it builds its own dict and passes json.dumps() of
        it as the message). Rather than refactor that call site, this
        unpacks the shape -- audit()'s field set and levels stay unchanged.
    """

    def format(self, record: logging.LogRecord) -> str:
        raw_message = record.getMessage()

        payload: dict[str, object] = {}
        parsed = _try_parse_json_object(raw_message)
        if parsed is not None:
            payload.update(parsed)
        else:
            payload["message"] = raw_message

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        # Applied last so nothing above can shadow these.
        payload["timestamp"] = datetime.fromtimestamp(
            record.created, tz=UTC
        ).isoformat()
        payload["level"] = record.levelname
        payload["logger"] = record.name

        return json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)


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
    """
    settings = get_settings()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers = [handler]

    logging.getLogger("dodeal_ai").setLevel(settings.log_level)
