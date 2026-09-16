"""The service app with the gate-chain probe mounted, for the security tests.

Register item 93: create_app() never mounts api/routes/_probe.py, so the three
modules that assert the gate chain against /_probe/protected build their own
app here instead of reaching into the one a deployment serves.
"""

from __future__ import annotations

from fastapi import FastAPI

from dodeal_ai.api.routes import _probe
from dodeal_ai.main import create_app


def probe_app() -> FastAPI:
    """A fresh service app, identical to the served one, plus the probe route."""
    app = create_app()
    app.include_router(_probe.router)
    return app
