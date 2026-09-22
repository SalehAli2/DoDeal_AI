"""The brief's CRM read clients (register item 143, ASSUMPTION[Q23]): keyset
pages read to the end, row-id order, never partial, and each row on its own."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.errors import BriefStoreUnavailable
from dodeal_ai.main import app
from dodeal_ai.tools import crm_reads
from dodeal_ai.tools.crm_reads import CrmJudgementStore, CrmUserDirectory
from dodeal_ai.tools.errors import BackendStatusError
from dodeal_ai.tools.keys import SettingsKeyResolver
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    JUDGEMENT_ROWS_PATH_ENV,
    FileJudgementStore,
    JudgementStoreError,
)
from dodeal_ai.units.structured_intelligence.user_directory import (
    USER_DIRECTORY_PATH_ENV,
)
from tests.helpers import tokens

SINCE = datetime(2026, 8, 21, tzinfo=UTC)
UNTIL = datetime(2026, 9, 20, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key="test-key",
        dd_api_keys={"tenant-a": "key-tenant-a"},
    )


def _row(row_id: int, **overrides: object) -> dict:
    row: dict = {
        "id": row_id,
        "note_id": row_id,
        "lead_id": 9000 + row_id,
        "author_id": 501,
        "note_created_at": "2026-09-01T10:00:00+04:00",
        "note_type": "discovery",
        "band": "good",
        "total": 72,
        "denominator": 80,
        "suppressed_reason": None,
        "prompt_sent": False,
        "enforcement_verdict": "allow",
        "rubric_version": "note_rubric_v2",
        "prompt_version": "unit_a_prompts_v3",
        "model_version": "invented-model-1",
        "config_version": "tenant-cfg-default-4",
    }
    row.update(overrides)
    return row


class PagedTransport:
    """Serves `rows` a page at a time by keyset, recording every URL. A page
    listed in `fail_pages` (1-based) raises the way a dead backend does."""

    def __init__(
        self,
        rows: list[dict],
        *,
        fail_pages: frozenset[int] = frozenset(),
        shuffle: bool = False,
        status: int = 503,
    ) -> None:
        self.rows = sorted(rows, key=lambda row: row["id"])
        self.fail_pages = fail_pages
        self.shuffle = shuffle
        self.status = status
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    async def get_json(self, url: str, headers: dict[str, str]) -> object:
        self.urls.append(url)
        self.headers.append(headers)
        page_number = len({u for u in self.urls})
        if page_number in self.fail_pages:
            raise BackendStatusError(self.status)
        query = parse_qs(urlsplit(url).query)
        after = int(query["after_id"][0]) if "after_id" in query else 0
        limit = int(query["limit"][0])
        page = [row for row in self.rows if row["id"] > after][:limit]
        if self.shuffle:
            page = random.Random(len(self.urls)).sample(page, len(page))
        return {"status": True, "data": page}


def _store(transport: PagedTransport) -> CrmJudgementStore:
    settings = _settings()
    return CrmJudgementStore(
        transport, SettingsKeyResolver(settings), settings, jitter=lambda: 0.0
    )


@pytest.fixture
def small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crm_reads, "PAGE_SIZE", 2)


# --- the guards ------------------------------------------------------------


async def test_three_pages_are_all_read(small_pages) -> None:
    """2 + 2 + 1: the short third page ends the read, and nothing is missed."""
    transport = PagedTransport([_row(n) for n in range(1, 6)])
    rows = await _store(transport).rows_between("tenant-a", since=SINCE, until=UNTIL)
    assert [row.note_id for row in rows] == [1, 2, 3, 4, 5]
    assert len(transport.urls) == 3
    assert "after_id=2" in transport.urls[1] and "after_id=4" in transport.urls[2]


async def test_more_than_the_maximum_is_503_never_partial() -> None:
    """10001 rows: refused, not truncated to the first 10000."""
    transport = PagedTransport([_row(n) for n in range(1, crm_reads.MAX_ROWS + 2)])
    with pytest.raises(BriefStoreUnavailable):
        await _store(transport).rows_between("tenant-a", since=SINCE, until=UNTIL)


async def test_exactly_the_maximum_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap refuses above it, not at it."""
    monkeypatch.setattr(crm_reads, "MAX_ROWS", 4)
    monkeypatch.setattr(crm_reads, "PAGE_SIZE", 3)
    transport = PagedTransport([_row(n) for n in range(1, 5)])
    rows = await _store(transport).rows_between("tenant-a", since=SINCE, until=UNTIL)
    assert len(rows) == 4


async def test_a_failed_second_page_is_503(small_pages, caplog) -> None:
    """The first page read fine and is thrown away: never a partial answer."""
    transport = PagedTransport([_row(n) for n in range(1, 6)], fail_pages={2})
    with pytest.raises(BriefStoreUnavailable):
        await _store(transport).rows_between("tenant-a", since=SINCE, until=UNTIL)
    record = next(
        r for r in caplog.records if r.getMessage() == "brief_store_read_failed"
    )
    assert record.kind == "unavailable"  # type: ignore[attr-defined]


async def test_shuffled_pages_come_out_in_row_id_order(small_pages) -> None:
    """Rows are sorted by the CRM's row id before a JudgementRow is built."""
    transport = PagedTransport([_row(n) for n in range(1, 8)], shuffle=True)
    rows = await _store(transport).rows_between("tenant-a", since=SINCE, until=UNTIL)
    assert [row.note_id for row in rows] == list(range(1, 8))


# --- what is sent -----------------------------------------------------------


async def test_the_query_carries_offsets_the_author_and_the_key() -> None:
    """Bounds keep their offset (never Z), and the tenant's DD-API-KEY rides."""
    transport = PagedTransport([_row(1)])
    await _store(transport).rows_between(
        "tenant-a", since=SINCE, until=UNTIL, author_id=501
    )
    url = urlsplit(transport.urls[0])
    query = parse_qs(url.query)
    assert url.netloc == "tenant-a.dodealcrm.com"
    assert url.path == "/api/service/judgements"
    assert query["since"] == ["2026-08-21T00:00:00+00:00"]
    assert query["until"] == ["2026-09-20T00:00:00+00:00"]
    assert query["author_id"] == ["501"]
    assert query["limit"] == [str(crm_reads.PAGE_SIZE)]
    assert transport.headers[0] == {"DD-API-KEY": "key-tenant-a"}


async def test_the_rows_field_set_is_unchanged() -> None:
    """The wire row is JudgementRow plus `id`; the id never reaches the row."""
    transport = PagedTransport([_row(1)])
    (row,) = await _store(transport).rows_between("tenant-a", since=SINCE, until=UNTIL)
    assert not hasattr(row, "id")


async def test_bad_rows_are_dropped_and_counted_not_the_page(caplog) -> None:
    """No id, a bad id, and a row JudgementRow refuses: three dropped, one kept."""
    page = [_row(1), _row(2, total=500), {"note_id": 3}, {"id": "x"}, "not a row"]

    class OnePage(PagedTransport):
        async def get_json(self, url: str, headers: dict[str, str]) -> object:
            return {"status": True, "data": page}

    kept = await _store(OnePage([])).rows_between("tenant-a", since=SINCE, until=UNTIL)
    assert [row.note_id for row in kept] == [1]
    counts = [
        r.count  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "backend_rejected_rows"
    ]
    assert counts == [3, 1]


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (422, "rejected"),
        (500, "unavailable"),
    ],
)
async def test_every_refusal_is_503_with_its_kind(status, kind, caplog) -> None:
    """Whatever the CRM said, the brief is unavailable; the log says which."""
    transport = PagedTransport([_row(1)], fail_pages={1}, status=status)
    with pytest.raises(BriefStoreUnavailable):
        await _store(transport).rows_between("tenant-a", since=SINCE, until=UNTIL)
    record = next(
        r for r in caplog.records if r.getMessage() == "brief_store_read_failed"
    )
    assert record.kind == kind  # type: ignore[attr-defined]


async def test_an_envelope_that_is_not_one_is_503(caplog) -> None:
    class Bare(PagedTransport):
        async def get_json(self, url: str, headers: dict[str, str]) -> object:
            return [_row(1)]

    with pytest.raises(BriefStoreUnavailable):
        await _store(Bare([])).rows_between("tenant-a", since=SINCE, until=UNTIL)


async def test_a_tenant_with_no_key_is_503_before_any_call() -> None:
    transport = PagedTransport([_row(1)])
    with pytest.raises(BriefStoreUnavailable):
        await _store(transport).rows_between("tenant-b", since=SINCE, until=UNTIL)
    assert transport.urls == []


async def test_a_page_that_does_not_move_the_key_is_503(small_pages) -> None:
    """A backend ignoring after_id would loop for ever; it is refused instead."""

    class Stuck(PagedTransport):
        async def get_json(self, url: str, headers: dict[str, str]) -> object:
            self.urls.append(url)
            return {"status": True, "data": [_row(1), _row(2)]}

    with pytest.raises(BriefStoreUnavailable):
        await _store(Stuck([])).rows_between("tenant-a", since=SINCE, until=UNTIL)


async def test_bad_bounds_are_refused_as_the_file_store_refuses_them() -> None:
    store = _store(PagedTransport([]))
    with pytest.raises(JudgementStoreError):
        await store.rows_between(
            "tenant-a", since=SINCE.replace(tzinfo=None), until=UNTIL
        )
    with pytest.raises(JudgementStoreError):
        await store.rows_between("tenant-a", since=UNTIL, until=SINCE)


async def test_the_directory_pages_the_same_way(small_pages) -> None:
    """Users by keyset too, each validated on its own."""
    people = [
        {"id": 1, "user_id": 501, "name": "Idris Vale", "role": "rep", "team": "north"},
        {"id": 2, "user_id": 401, "name": "Ayla Brook", "role": "head_of_sales"},
        {"id": 3, "user_id": 502, "name": "Noor", "role": "rep", "team": None},
    ]
    settings = _settings()
    directory = CrmUserDirectory(
        PagedTransport(people), SettingsKeyResolver(settings), settings
    )
    users = await directory.users("tenant-a")
    assert [user.user_id for user in users] == [501, 401]


# --- the lifespan wiring ----------------------------------------------------


def test_brief_source_crm_wires_both_clients_and_a_file_still_wins(
    monkeypatch, tmp_path
) -> None:
    """crm fills what the files did not; a set file path keeps its fake."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    monkeypatch.setenv("DODEAL_BRIEF_SOURCE", "crm")
    monkeypatch.delenv(USER_DIRECTORY_PATH_ENV, raising=False)
    rows = tmp_path / "rows.json"
    rows.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(JUDGEMENT_ROWS_PATH_ENV, str(rows))
    get_settings.cache_clear()
    with TestClient(app):
        assert isinstance(app.state.judgement_store, FileJudgementStore)
        assert isinstance(app.state.user_directory, CrmUserDirectory)
    monkeypatch.delenv(JUDGEMENT_ROWS_PATH_ENV)
    get_settings.cache_clear()
    with TestClient(app):
        assert isinstance(app.state.judgement_store, CrmJudgementStore)
    get_settings.cache_clear()


def test_the_default_source_is_none() -> None:
    assert Settings(_env_file=None, jwt_signing_key="k").brief_source == "none"


def test_since_is_never_sent_as_z() -> None:
    """ASSUMPTION[Q5]'s rule: an explicit offset, so a naive comparison fails
    visibly rather than shifting by the offset."""
    assert "Z" not in (SINCE + timedelta(0)).isoformat()


async def test_a_page_that_fails_once_is_retried_with_the_default_jitter() -> None:
    """The one retry leads.py's rule allows: a 503, then the page."""

    class OnceDown(PagedTransport):
        async def get_json(self, url: str, headers: dict[str, str]) -> object:
            self.urls.append(url)
            if len(self.urls) == 1:
                raise BackendStatusError(503)
            return {"status": True, "data": [_row(1)]}

    settings = _settings()
    transport = OnceDown([])
    store = CrmJudgementStore(transport, SettingsKeyResolver(settings), settings)
    rows = await store.rows_between("tenant-a", since=SINCE, until=UNTIL)
    assert [row.note_id for row in rows] == [1]
    assert len(transport.urls) == 2
