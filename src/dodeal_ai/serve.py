"""The container entrypoint (register item 94): `python -m dodeal_ai.serve`.

uvicorn with --proxy-headers, trusting forwarded headers only from
DODEAL_FORWARDED_ALLOW_IPS, and a 30 s graceful shutdown. Read from Settings so
the trusted proxies have one validated source, and a missing signing key
refuses here as it would at startup.
"""

from __future__ import annotations

import uvicorn

from dodeal_ai.core.config import get_settings

APP = "dodeal_ai.main:app"
HOST = "0.0.0.0"  # inside the container; the port is published, never the host
PORT = 8000
# In-flight judgements get this long after SIGTERM before uvicorn closes them.
# Above the 25 s judgement deadline, so a draining pod finishes what it admitted.
GRACEFUL_SHUTDOWN_SECONDS = 30


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        APP,
        host=HOST,
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":  # pragma: no cover - the process entry, run by Docker
    main()
