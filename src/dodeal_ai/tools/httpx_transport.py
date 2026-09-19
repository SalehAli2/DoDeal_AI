"""Production transport over the ONE CRM httpx.AsyncClient the lifespan owns.

Register item 4: the pool limits and the timeout live on that client and come
from Settings (main.py), so this module holds no number of its own. Not
exercised by unit tests; the integration lane runs it against a fake backend.
"""

from __future__ import annotations

import httpx

from dodeal_ai.tools.errors import BackendStatusError, retry_after_seconds


class HttpxTransport:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get_json(self, url: str, headers: dict[str, str]) -> object:
        response = await self._client.get(url, headers=headers)
        if not response.is_success:
            # The status and Retry-After only: the body is the CRM's and may
            # quote the request (register item 89).
            raise BackendStatusError(
                response.status_code,
                retry_after=retry_after_seconds(response.headers.get("Retry-After")),
            )
        return response.json()
