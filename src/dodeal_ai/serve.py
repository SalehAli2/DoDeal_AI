"""The container entrypoint (register item 94): `python -m dodeal_ai.serve`.

uvicorn with --proxy-headers, trusting forwarded headers only from
DODEAL_FORWARDED_ALLOW_IPS, and a graceful shutdown derived from the judgement
deadline (30 s at the default 25 s deadline). Read from Settings so
the trusted proxies have one validated source, and a missing signing key
refuses here as it would at startup.
"""

from __future__ import annotations

import math

import uvicorn

from dodeal_ai.core.config import Settings, get_settings

APP = "dodeal_ai.main:app"
HOST = "0.0.0.0"  # inside the container; the port is published, never the host
PORT = 8000
# How much longer than the judgement deadline a draining pod waits after
# SIGTERM (register item 94): time to write the answer and release the slot.
SHUTDOWN_MARGIN_SECONDS = 5


def graceful_shutdown_seconds(settings: Settings) -> int:
    """The drain: the judgement deadline plus the margin, rounded UP, so a pod
    finishes what it admitted even when the deadline is raised."""
    return math.ceil(settings.judgement_deadline_seconds + SHUTDOWN_MARGIN_SECONDS)


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        APP,
        host=HOST,
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_graceful_shutdown=graceful_shutdown_seconds(settings),
    )


if __name__ == "__main__":  # pragma: no cover - the process entry, run by Docker
    main()
