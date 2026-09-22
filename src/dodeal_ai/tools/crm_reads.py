"""Read clients for the brief's two sources: stored judgements and the user
directory, from the CRM (register item 143).

ASSUMPTION[Q23]: the CRM will serve two keyset-paged reads on its service
surface, as written in docs/contracts/crm_service_reads.yaml:

  GET /api/service/judgements?since&until&author_id&after_id&limit
  GET /api/service/users?after_id&limit

Neither exists yet. Every row carries the CRM's row `id`; a page is
`{"status": true, "data": [...]}` and the read continues from the largest id
seen until a page comes back short. The field set of a judgement row is
exactly JudgementRow's, which does not change here. Off by default
(`brief_source="none"`), and the file fakes still win when their paths are set.

Built like tools/leads.py: the tenant's DD-API-KEY, the one retry rule
(`backend_retry_delay`), typed failures, and each row validated on its own --
a bad row is dropped and counted, never the page.

NEVER PARTIAL. Any page that fails, or more than MAX_ROWS rows, is 503
brief_store_unavailable. A measure computed over half a month would be a wrong
statement that looks right, which is worse than no brief.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from datetime import datetime
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, ValidationError

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.errors import BriefStoreUnavailable
from dodeal_ai.core.resilience import ExternalCallError, call_with_watchdog
from dodeal_ai.core.validation import OutputValidationError, validate_output
from dodeal_ai.tools.errors import BackendStatusError
from dodeal_ai.tools.keys import BackendKeyError, TenantKeyResolver
from dodeal_ai.tools.leads import (
    RETRY_JITTER_MAX_SECONDS,
    RETRY_JITTER_MIN_SECONDS,
    Transport,
    backend_retry_delay,
)
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    JudgementRow,
    JudgementStoreError,
)
from dodeal_ai.units.structured_intelligence.user_directory import User

_logger = logging.getLogger("dodeal_ai.tools")

GET_JUDGEMENTS_LABEL = "tool.get_judgements"
GET_USERS_LABEL = "tool.get_users"

# Rows asked for per page. 500 keeps a month of a busy tenant to a few pages;
# the CRM may return fewer, and a short page is what ends the read.
PAGE_SIZE = 500

# The most rows one read may assemble before it refuses. Above it the brief is
# 503 rather than computed over a truncated set.
MAX_ROWS = 10_000


class KeysetPage(BaseModel):
    """One page: rows still raw, validated one at a time below."""

    model_config = ConfigDict(extra="ignore")

    status: bool
    data: list[object]


def _jitter() -> float:
    return random.uniform(RETRY_JITTER_MIN_SECONDS, RETRY_JITTER_MAX_SECONDS)


class _Refused(Exception):
    """A read that cannot be finished whole. `kind` is a fixed word for the log."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


def _status_kind(status: int) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    return "rejected" if 400 <= status < 500 else "unavailable"


class _KeysetReader:
    """The shared half: key, URL, retry, envelope, paging, row ids."""

    def __init__(
        self,
        transport: Transport,
        key_resolver: TenantKeyResolver,
        settings: Settings | None = None,
        *,
        jitter: Callable[[], float] = _jitter,
    ) -> None:
        self._transport = transport
        self._key_resolver = key_resolver
        self._settings = settings or get_settings()
        self._jitter = jitter

    def _url(self, tenant: str, path: str, query: dict[str, object]) -> str:
        s = self._settings
        base = f"{s.backend_scheme}://{tenant}.{s.backend_base_domain}/api/service"
        return f"{base}/{path}?{urlencode(query)}"

    async def _page(
        self, url: str, headers: dict[str, str], label: str
    ) -> list[object]:
        async def _fetch() -> object:
            return await self._transport.get_json(url, headers)

        try:
            raw = await call_with_watchdog(
                _fetch,
                label=label,
                retry_delay=lambda exc: backend_retry_delay(exc, self._jitter),
            )
        except ExternalCallError as exc:
            cause = exc.cause
            raise _Refused(
                _status_kind(cause.status)
                if isinstance(cause, BackendStatusError)
                else "unavailable"
            ) from None
        try:
            return validate_output(KeysetPage, raw, label=label).data
        except OutputValidationError:
            raise _Refused("envelope_invalid") from None

    async def read_all(
        self, tenant: str, path: str, params: dict[str, object], label: str
    ) -> list[dict]:
        """Every row, in row-id order, or BriefStoreUnavailable. Never partial."""
        try:
            return await self._read_all(tenant, path, params, label)
        except _Refused as refused:
            _logger.warning(
                "brief_store_read_failed",
                extra={
                    "reason_code": "brief_store_unavailable",
                    "tenant": tenant,
                    "label": label,
                    "kind": refused.kind,
                },
            )
            raise BriefStoreUnavailable() from None

    async def _read_all(
        self, tenant: str, path: str, params: dict[str, object], label: str
    ) -> list[dict]:
        try:
            key = self._key_resolver.resolve(tenant)
        except BackendKeyError:
            raise _Refused("key_missing") from None
        headers = {"DD-API-KEY": key.get_secret_value()}
        rows: dict[int, dict] = {}
        dropped = 0
        after_id: int | None = None
        while True:
            query = {**params, "limit": PAGE_SIZE}
            if after_id is not None:
                query["after_id"] = after_id
            page = await self._page(self._url(tenant, path, query), headers, label)
            for entry in page:
                row_id = entry.get("id") if isinstance(entry, dict) else None
                if type(row_id) is not int or row_id < 1:
                    dropped += 1
                    continue
                assert isinstance(entry, dict)
                rows[row_id] = {k: v for k, v in entry.items() if k != "id"}
            if len(rows) > MAX_ROWS:
                raise _Refused("too_many_rows")
            if len(page) < PAGE_SIZE:
                break
            largest = max(rows, default=0)
            if after_id is not None and largest <= after_id:
                # A full page that moved the key nowhere would loop for ever.
                raise _Refused("paging_stalled")
            after_id = largest
        if dropped:
            _rejected(dropped, label)
        return [rows[row_id] for row_id in sorted(rows)]


def _rejected(count: int, label: str) -> None:
    """One WARNING for the rows dropped: the count and the read, never a value."""
    _logger.warning(
        "backend_rejected_rows",
        extra={
            "reason_code": "backend_rejected_rows",
            "count": count,
            "endpoint": label,
        },
    )


def _validated[M: BaseModel](schema: type[M], raws: list[dict], label: str) -> list[M]:
    """Each row on its own; a bad one is dropped and counted (item 90)."""
    valid: list[M] = []
    for raw in raws:
        try:
            valid.append(schema.model_validate(raw))
        except ValidationError:
            continue
    if len(valid) < len(raws):
        _rejected(len(raws) - len(valid), label)
    return valid


class CrmJudgementStore(_KeysetReader):
    """A JudgementStore over GET /api/service/judgements. ASSUMPTION[Q23]."""

    async def rows_between(
        self,
        tenant: str,
        *,
        since: datetime,
        until: datetime,
        author_id: int | None = None,
    ) -> Sequence[JudgementRow]:
        """The Protocol's read. Bounds are sent with their offset, never `Z`,
        for the reason ASSUMPTION[Q5] gives; oldest first by row id."""
        if since.tzinfo is None or until.tzinfo is None:
            raise JudgementStoreError("since and until must be timezone-aware")
        if since > until:
            raise JudgementStoreError("since is after until")
        params: dict[str, object] = {
            "since": since.isoformat(),
            "until": until.isoformat(),
        }
        if author_id is not None:
            params["author_id"] = author_id
        raws = await self.read_all(tenant, "judgements", params, GET_JUDGEMENTS_LABEL)
        return _validated(JudgementRow, raws, GET_JUDGEMENTS_LABEL)


class CrmUserDirectory(_KeysetReader):
    """A UserDirectory over GET /api/service/users. ASSUMPTION[Q23]."""

    async def users(self, tenant: str) -> Sequence[User]:
        raws = await self.read_all(tenant, "users", {}, GET_USERS_LABEL)
        return _validated(User, raws, GET_USERS_LABEL)
