"""One TenantScope for tests that need an identity but are not testing one.

Every pass in Unit A now takes a `scope`, because that is what the token charge
is billed to (`core/cost/limiter.py::enforce_token_cost`). Most tests do not
care whose scope it is -- they care about a reprompt, or a schema, or a log
line -- and eight copies of the same six-line `RequestContext(...)` is how the
tenant in one of them quietly stops matching the tenant in its assertions.

Built through `RequestContext.scope()`, never by calling `TenantScope(...)`
directly: that is the one construction path, and a test that took a shortcut
around it would be exercising a scope nobody verified.
"""

from __future__ import annotations

from dodeal_ai.core.context import RequestContext, TenantScope


# Not named test_*: a helper imported into a test module under that name is
# collected there as a test (register item 152).
def make_scope(
    tenant: str = "tenant-a",
    subject: str = "42",
    request_id: str = "req-1",
) -> TenantScope:
    """A verified-looking scope for a test that needs one and asserts nothing
    about it."""
    return RequestContext(
        tenant=tenant,
        subject=subject,
        database=f"crm_{tenant.replace('-', '_')}",
        roles=(),
        permissions=frozenset(),
        request_id=request_id,
    ).scope()


TEST_SCOPE = make_scope()


def history_scope(tenant: str = "tenant-a", author_id: int = 7) -> TenantScope:
    """A service-principal scope charged to the HISTORY budget (item 127)."""
    return RequestContext(
        tenant=tenant,
        subject="service",
        database="",
        roles=(),
        permissions=frozenset(),
        request_id="req-history",
        principal="service",
    ).scope_for_author(author_id, budget="history")
