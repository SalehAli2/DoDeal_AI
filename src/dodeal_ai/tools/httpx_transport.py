"""Production transport backed by httpx.AsyncClient.

Not exercised by unit tests (no live network in the suite). Provided so the
client has a real transport to use once the backend endpoint and per-tenant key
are provisioned.
"""

from __future__ import annotations

import httpx

from dodeal_ai.tools.errors import BackendStatusError, retry_after_seconds


class HttpxTransport:
    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout

    async def get_json(self, url: str, headers: dict[str, str]) -> object:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, headers=headers)
            if not response.is_success:
                # The status and Retry-After only: the body is the CRM's and may
                # quote the request (register item 89).
                raise BackendStatusError(
                    response.status_code,
                    retry_after=retry_after_seconds(
                        response.headers.get("Retry-After")
                    ),
                )
            return response.json()
