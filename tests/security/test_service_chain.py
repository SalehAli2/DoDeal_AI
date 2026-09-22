"""The service gate chain over HTTP (register item D1): the CRM's token passes,
a user token does not, the Host still decides the tenant, and Gate 4 counts
the tenant alone."""

from __future__ import annotations

import pytest
from fakeredis import FakeServer
from fakeredis import aioredis as fake_aioredis
from fastapi.testclient import TestClient

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import PrincipalMismatchError, RequestContext
from dodeal_ai.core.cost.limiter import (
    _INCR_TENANT_SCRIPT,
    CostLimitError,
    enforce_tenant_cost,
)
from tests.conftest import RedisFakes
from tests.helpers import tokens
from tests.helpers.probe_app import probe_app

app = probe_app()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _headers(token: str, tenant: str = "tenant-a") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Host": f"{tenant}.dodealcrm.com"}


def _audit(json_log, gate: str) -> list[dict]:
    return [line for line in json_log() if line.get("gate") == gate]


def test_a_service_token_passes_the_service_chain(client) -> None:
    """The CRM's token reaches the route with a service principal."""
    response = client.get(
        "/_probe/service", headers=_headers(tokens.mint_service_token())
    )
    assert response.status_code == 200
    assert response.json()["principal"] == "service"
    assert response.json()["subject"] == "service"
    assert response.json()["tenant"] == "tenant-a"


def test_a_user_token_is_401_on_the_service_chain(client, json_log) -> None:
    """A person's token never passes as the CRM's, and the audit says why."""
    response = client.get("/_probe/service", headers=_headers(tokens.mint_token()))
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    denied = _audit(json_log, "service_auth")
    assert [line["reason_code"] for line in denied] == ["user_token"]
    assert denied[0]["tenant"] is None


def test_a_service_token_is_401_on_the_user_chain(client) -> None:
    """The reverse holds too: the user verifier shares no key with the service."""
    response = client.get(
        "/_probe/protected", headers=_headers(tokens.mint_service_token())
    )
    assert response.status_code == 401


def test_a_host_for_another_tenant_is_403(client, json_log) -> None:
    """The Host must name the token's tenant, exactly as on the user chain."""
    token = tokens.mint_service_token(subdomain="tenant-a")
    response = client.get("/_probe/service", headers=_headers(token, "tenant-b"))
    assert response.status_code == 403
    denied = _audit(json_log, "service_tenancy")
    assert [line["reason_code"] for line in denied] == ["tenant_mismatch"]


def test_only_the_tenant_request_counter_moves(client, redis_fakes: RedisFakes) -> None:
    """The service chain's Gate 4 writes cost:tenant and no user key."""
    for _ in range(2):
        client.get("/_probe/service", headers=_headers(tokens.mint_service_token()))
    assert redis_fakes.cost.store == {"cost:tenant:tenant-a": 2}
    assert redis_fakes.cost.keys_for(_INCR_TENANT_SCRIPT) == {"cost:tenant:tenant-a"}


def test_the_tenant_cap_is_429_on_the_service_chain(
    client, monkeypatch: pytest.MonkeyPatch, redis_fakes: RedisFakes
) -> None:
    """Over the tenant cap the service chain refuses as Gate 4 always has."""
    monkeypatch.setenv("DODEAL_COST_PER_TENANT_LIMIT", "1")
    get_settings.cache_clear()
    first = client.get("/_probe/service", headers=_headers(tokens.mint_service_token()))
    second = client.get(
        "/_probe/service", headers=_headers(tokens.mint_service_token())
    )
    assert (first.status_code, second.status_code) == (200, 429)
    assert second.json() == {"detail": "Too Many Requests"}


# --- the limiter half -------------------------------------------------------


async def test_the_tenant_counter_fails_open(redis_fakes: RedisFakes, caplog) -> None:
    """A dead cost store allows the request and says so."""
    redis_fakes.cost.fail = True
    await enforce_tenant_cost("tenant-a")
    assert "cost_cap_bypassed" in caplog.text


async def test_the_tenant_counter_denies_over_the_cap(
    monkeypatch: pytest.MonkeyPatch, redis_fakes: RedisFakes
) -> None:
    """The first call past the cap is refused with the tenant reason."""
    monkeypatch.setenv("DODEAL_COST_PER_TENANT_LIMIT", "2")
    get_settings.cache_clear()
    await enforce_tenant_cost("tenant-a", amount=2)
    with pytest.raises(CostLimitError) as raised:
        await enforce_tenant_cost("tenant-a")
    assert raised.value.reason_code == "tenant_quota_exceeded"


async def test_the_tenant_script_runs_on_real_lua_and_sets_a_window() -> None:
    """Executed Lua: the counter moves, returns its total, and gains a window."""
    fake = fake_aioredis.FakeRedis(server=FakeServer(), decode_responses=True)
    key = "cost:tenant:tenant-a"
    assert await fake.eval(_INCR_TENANT_SCRIPT, 1, key, 3, 60) == [3]
    assert await fake.eval(_INCR_TENANT_SCRIPT, 1, key, 1, 60) == [4]
    assert 0 < await fake.ttl(key) <= 60
    await fake.persist(key)
    await fake.eval(_INCR_TENANT_SCRIPT, 1, key, 1, 60)
    assert await fake.ttl(key) > 0
    await fake.aclose()


# --- scope_for_author -------------------------------------------------------


def _context(principal: str) -> RequestContext:
    return RequestContext(
        tenant="tenant-a",
        subject="service",
        database="",
        roles=(),
        permissions=frozenset(),
        request_id="req-1",
        principal=principal,  # type: ignore[arg-type]
    )


def test_scope_for_author_names_the_author_as_an_asserted_subject() -> None:
    """The subject is author:<id>, marked asserted, under the service principal."""
    scope = _context("service").scope_for_author(7)
    assert (scope.subject, scope.subject_asserted, scope.principal) == (
        "author:7",
        True,
        "service",
    )
    assert scope.token_budget == "live"
    assert _context("service").scope_for_author(7, budget="history").token_budget == (
        "history"
    )


def test_scope_for_author_refuses_a_user_principal() -> None:
    """A person's token may never produce a scope naming somebody else."""
    with pytest.raises(PrincipalMismatchError):
        _context("user").scope_for_author(7)


def test_a_user_scope_keeps_the_three_defaults() -> None:
    """scope() on a user context is unchanged: user, not asserted, live."""
    scope = _context("user").scope()
    assert (scope.principal, scope.subject_asserted, scope.token_budget) == (
        "user",
        False,
        "live",
    )
