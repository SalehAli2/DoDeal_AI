"""The body cap per route (middleware/body_limit.py, L7): POST
/api/v1/calls/reanalysis, which carries a whole transcript, is bounded by
MAX_REANALYSIS_BODY_BYTES (1 MB); every other request by the default cap, on
the header path and the streamed path alike."""

from __future__ import annotations

import pytest

from dodeal_ai.core.config import Settings
from dodeal_ai.main import app
from dodeal_ai.middleware import body_limit
from dodeal_ai.middleware.body_limit import REANALYSIS_ROUTE, BodyLimitMiddleware
from tests.unit.test_body_limit import (
    BodyReader,
    _chunks,
    _collect,
    _http_scope,
    _status,
)

DEFAULT_CAP = 64
REANALYSIS_CAP = 256
METHOD, PATH = REANALYSIS_ROUTE


@pytest.fixture(autouse=True)
def _two_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        body_limit,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            jwt_signing_key="test-key",
            max_request_body_bytes=DEFAULT_CAP,
            max_reanalysis_body_bytes=REANALYSIS_CAP,
        ),
    )


def test_the_default_reanalysis_cap_is_1_mb() -> None:
    settings = Settings(_env_file=None, jwt_signing_key="k")
    assert settings.max_reanalysis_body_bytes == 1_048_576


def test_the_capped_route_is_the_apps_reanalysis_route() -> None:
    """The constant names a route that exists, so it cannot drift silently."""
    assert METHOD.lower() in app.openapi()["paths"][PATH]


async def _status_for(method: str, path: str, size: int, *, declared: bool) -> int:
    receive, _ = _chunks(b"x" * size)
    send, messages = _collect()
    headers = [(b"content-length", str(size).encode())] if declared else []
    scope = _http_scope(method=method, path=path, headers=headers)
    await BodyLimitMiddleware(BodyReader())(scope, receive, send)
    return _status(messages)


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize(
    ("method", "path", "size", "status"),
    [
        (METHOD, PATH, REANALYSIS_CAP, 200),
        (METHOD, PATH, REANALYSIS_CAP + 1, 413),
        (METHOD, "/api/v1/calls/jobs", DEFAULT_CAP + 1, 413),
        (METHOD, "/api/v1/calls/jobs", DEFAULT_CAP, 200),
        ("PUT", PATH, DEFAULT_CAP + 1, 413),
        (METHOD, f"{PATH}/", DEFAULT_CAP + 1, 413),
    ],
)
async def test_only_reanalysis_takes_the_larger_cap(
    method: str, path: str, size: int, status: int, declared: bool
) -> None:
    assert await _status_for(method, path, size, declared=declared) == status
