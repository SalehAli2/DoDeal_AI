import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from dodeal_ai.api.routes import admin, calls, judgements, measures
from dodeal_ai.core import metrics as service_metrics
from dodeal_ai.core.config import (
    REDIS_POOL_HEADROOM,
    ConfigError,
    Settings,
    get_settings,
)
from dodeal_ai.core.errors import register_error_handlers
from dodeal_ai.core.llm import build_llm_client
from dodeal_ai.core.logging_config import configure_logging
from dodeal_ai.core.prompting import clear_templates, preload_templates
from dodeal_ai.core.redis import (
    check_cost_redis_ready,
    check_jobs_redis_ready,
    check_operational_redis_ready,
    get_cost_client,
    get_jobs_client,
    get_operational_client,
    get_queue_client,
)
from dodeal_ai.core.tenant_config import (
    clear_tenant_configs,
    load_tenant_configs,
    register_section_parsers,
)
from dodeal_ai.middleware.body_limit import BodyLimitMiddleware
from dodeal_ai.middleware.inflight import InflightMiddleware
from dodeal_ai.middleware.request_id import RequestIDMiddleware
from dodeal_ai.tools.crm_reads import CrmJudgementStore, CrmUserDirectory
from dodeal_ai.tools.httpx_transport import HttpxTransport
from dodeal_ai.tools.keys import get_key_resolver
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.queues import refresh_queue_depths
from dodeal_ai.units.structured_intelligence.config import (
    UNIT_A_SECTION,
    parse_unit_a_section,
)
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    JudgementStoreError,
    load_configured_store,
)
from dodeal_ai.units.structured_intelligence.templates import UNIT_A_TEMPLATES
from dodeal_ai.units.structured_intelligence.user_directory import (
    UserDirectoryError,
    load_configured_directory,
)

# Model calls one judgement can have in flight AT ONCE: classify runs alone,
# then vague and score are gathered (units/structured_intelligence/pipeline.py).
# Sized against PASSES and not requests -- a pool of max_inflight would make the
# second gathered pass queue for a connection INSIDE its own timeout, turning a
# pool wait into a model timeout and a 503.
LLM_CALLS_PER_JUDGEMENT = 2

# Every unit's section in a `<tenant>.json` and the parser that validates it (F2).
TENANT_CONFIG_SECTIONS = {
    UNIT_A_SECTION: parse_unit_a_section,
    UNIT_B_SECTION: parse_unit_b_section,
}

# Backend reads one judgement has in flight at once: the lead and its notes,
# fetched together (register item 9). The CRM pool is sized on it (item 4).
BACKEND_CALLS_PER_JUDGEMENT = 2


def _llm_limits(settings: Settings) -> httpx.Limits:
    """Pool bounds derived from max_inflight, the way core/redis.py derives its
    own from the same number. Keepalive is set to the full pool because every
    connection goes to ONE host: a capped keepalive would make the pool
    re-handshake TLS under exactly the load it was widened for."""
    ceiling = settings.max_inflight * LLM_CALLS_PER_JUDGEMENT
    return httpx.Limits(max_connections=ceiling, max_keepalive_connections=ceiling)


def _crm_client(settings: Settings) -> httpx.AsyncClient:
    """The ONE CRM client (register item 4): pooled per admitted judgement's two
    reads, timed out by external_call_timeout_seconds, never a literal."""
    ceiling = settings.max_inflight * BACKEND_CALLS_PER_JUDGEMENT
    return httpx.AsyncClient(
        limits=httpx.Limits(max_connections=ceiling, max_keepalive_connections=ceiling),
        timeout=httpx.Timeout(settings.external_call_timeout_seconds),
    )


def _load_brief_sources(app: FastAPI) -> None:
    """The judgement store and the user directory, read once here (register
    item 155) so no brief reads disk. A bad source refuses startup naming the
    STORE, never its path; the error is not chained, so its text is not shown."""
    try:
        app.state.judgement_store = load_configured_store()
    except JudgementStoreError:
        clear_templates()
        clear_tenant_configs()
        raise ConfigError("judgement_store_invalid") from None
    try:
        app.state.user_directory = load_configured_directory()
    except UserDirectoryError:
        clear_templates()
        clear_tenant_configs()
        raise ConfigError("user_directory_invalid") from None


def _crm_brief_sources(app: FastAPI, settings: Settings) -> None:
    """Register item 143: with DODEAL_BRIEF_SOURCE=crm, each source the files
    did not provide reads the CRM over the one pooled client. A file set by
    its variable still wins."""
    if settings.brief_source != "crm":
        return
    transport = HttpxTransport(app.state.crm_http)
    if app.state.judgement_store is None:
        app.state.judgement_store = CrmJudgementStore(
            transport, get_key_resolver(), settings
        )
    if app.state.user_directory is None:
        app.state.user_directory = CrmUserDirectory(
            transport, get_key_resolver(), settings
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed: if required config (signing key) is absent, refuse to start.
    settings = get_settings()
    configure_logging()
    # FIRST, before any socket or pool exists: a missing template must refuse
    # here, not on the first paid call days later. Read once for the app's life
    # so no judgement makes a disk read on the event loop (register item 85).
    preload_templates(UNIT_A_TEMPLATES)
    # Register item 97: the section parsers the runtime override store parses
    # with, then every tenant file validated before any socket exists.
    register_section_parsers(TENANT_CONFIG_SECTIONS)
    if settings.tenant_config_dir is not None:
        try:
            load_tenant_configs(settings.tenant_config_dir, TENANT_CONFIG_SECTIONS)
        except BaseException:
            clear_templates()
            raise
    _load_brief_sources(app)
    if not settings.dd_api_keys:
        # Not fail-closed: the gate chain and /ready must work before a key is
        # provisioned (Step 0). Loud so a deployment with no backend keys at
        # all is never silently discovered later as every data call 401ing.
        #
        # AFTER configure_logging(), deliberately (audit §6.9). Emitted before
        # it, this line went out through whatever handler logging happened to
        # have -- unformatted, and invisible to a collector that only parses the
        # JSON lines every other startup event uses. The loudest line in the
        # file was the one least likely to be seen.
        #
        # `event` on all four startup lines (register item 121): a collector
        # filters on a field, and the messages stay exactly as they were.
        logging.getLogger("dodeal_ai.startup").error(
            "backend_keys_missing count=0", extra={"event": "backend_keys_missing"}
        )

    if settings.service_jwt_signing_key is None:
        # Not a refusal, for the reason backend_keys_missing is not: /health
        # and /ready must answer before the CRM's key is provisioned. /ready
        # 503s while this holds, so the pod stays out of rotation (item D1).
        logging.getLogger("dodeal_ai.startup").error(
            "service_token_not_configured",
            extra={"event": "service_token_not_configured"},
        )

    if settings.backend_scheme == "http":
        # NOT a refusal, and never one: the demo needs http, so this is the
        # loudest thing short of not serving. ERROR rather than WARNING because
        # the consequence is a live credential on the wire, not degraded
        # behaviour. Names no key, no tenant and no URL -- the fact is the whole
        # message, and anything identifying would travel with it into the log.
        logging.getLogger("dodeal_ai.startup").error(
            "backend_scheme_insecure -- every backend request puts that "
            "tenant's DD-API-KEY on the wire in clear. Treat as key "
            "disclosure, not misconfiguration, unless this is the demo.",
            extra={"event": "backend_scheme_insecure"},
        )

    # An explicit pool below one connection per admitted request plus headroom
    # queues admitted requests on a healthy Redis (item 81). NOT a refusal: an
    # operator may size down on purpose; the line only makes it visible.
    required_pool = settings.max_inflight + REDIS_POOL_HEADROOM
    if (
        settings.redis_max_connections is not None
        and settings.redis_max_connections < required_pool
    ):
        logging.getLogger("dodeal_ai.startup").warning(
            "redis_pool_below_inflight",
            extra={
                "event": "redis_pool_below_inflight",
                "pool": settings.redis_max_connections,
                "required": required_pool,
            },
        )

    # ONE pooled client for every model call in the process. Built even when no
    # provider is configured: constructing it opens no socket, and closing it
    # unconditionally below keeps shutdown symmetrical -- the same argument the
    # Redis aclose() calls are made on.
    app.state.http = httpx.AsyncClient(limits=_llm_limits(settings))
    # The CRM's pool, beside the model's and closed on the same two paths.
    app.state.crm_http = _crm_client(settings)
    _crm_brief_sources(app, settings)
    if settings.llm_provider is None:
        # PERMISSIVE, for the same reason as backend_keys_missing above and in
        # the same shape: no provider is provisioned yet (Step 0), and a service
        # that refuses to start cannot serve /health, /ready or the gate chain.
        # /ready answers 503 while this is None, so the pod starts and stays OUT
        # of rotation rather than joining it and 500ing every judgement.
        app.state.llm = None
        logging.getLogger("dodeal_ai.startup").error(
            "llm_not_configured", extra={"event": "llm_not_configured"}
        )
    else:
        # Provider SET and anything else wrong -- no key, a provider with no
        # adapter, a profile that fails the sweep -- is a genuine ConfigError
        # and refuses to start. Somebody meant to configure this and got it
        # wrong; that is not a state to serve traffic in (item 84).
        try:
            app.state.llm = build_llm_client(settings, app.state.http)
        except BaseException:
            # A refused startup never reaches the cleanup after `yield`, so the
            # preloaded templates and the pool are released here, in shutdown's
            # order; bare `raise` keeps the original error.
            clear_templates()
            clear_tenant_configs()
            await app.state.http.aclose()
            await app.state.crm_http.aclose()
            raise
    yield
    app.state.judgement_store = None
    app.state.user_directory = None
    # Before the pools, because clearing a dict cannot fail: a cache that
    # outlived its app would serve this deployment's templates to the next one.
    clear_templates()
    clear_tenant_configs()
    # The model pool first, before the Redis pools: it is the one holding
    # sockets to a third party, and a client left unclosed leaks them.
    await app.state.http.aclose()
    await app.state.crm_http.aclose()
    # Release the connection pools on shutdown. Building a client opens no
    # socket, so constructing one here only to close it costs nothing, and
    # closing unconditionally keeps shutdown symmetrical whether or not the app
    # ever used it. The queue connection is arq's, closed by arq.
    #
    # close_connection_pool=True is REQUIRED now that core/redis.py hands each
    # client a pool of its own: redis-py sets auto_close_connection_pool False
    # for a caller-supplied pool, so a bare aclose() would return the one
    # checked-out connection and leak the other nineteen.
    await get_cost_client().aclose(close_connection_pool=True)
    await get_operational_client().aclose(close_connection_pool=True)
    await get_jobs_client().aclose(close_connection_pool=True)
    await get_queue_client().aclose(close_connection_pool=True)


async def health() -> dict[str, str]:
    return {"status": "ok"}


async def ready(request: Request):
    try:
        settings = get_settings()
    except ConfigError:
        return JSONResponse(status_code=503, content={"status": "not ready"})
    # STRICT, unlike the Redis fields below, and deliberately unlike them. There
    # is no fail-open path for a missing model: every judgement route 503s
    # without a client, so a pod in this state can serve /health and nothing
    # that matters. Answering 200 would put it in rotation to fail every
    # request, which is the defect item 84 exists to close.
    if getattr(request.app.state, "llm", None) is None:
        return JSONResponse(
            status_code=503, content={"status": "not ready", "llm": "not configured"}
        )
    # STRICT for the same reason (register item D1): with no service key every
    # direct, history, brief, measures and admin call is 401, which is most of
    # what this service is for.
    if settings.service_jwt_signing_key is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not ready", "service_token": "not configured"},
        )
    # Redis down does NOT take the pod out of rotation: the cost gate fails
    # OPEN on a Redis outage (core/cost/limiter.py), so the service still
    # serves requests correctly without it. Report the degraded state in the
    # body so it's observable, but keep 200 so an orchestrator doesn't pull a
    # functioning pod over a non-critical dependency.
    #
    # Both connections are probed and reported separately: a healthy cost store
    # and a dead idempotency store is a real state, and one flag would hide it.
    # Neither probe raises (core/redis.py swallows RedisError into False), so a
    # dependency being down cannot turn this endpoint into a 500.
    #
    # CONCURRENTLY, so two dead connections cost ONE connect timeout and not
    # two. Sequentially, the worst case is the sum of both probes, and a
    # readiness deadline sized against one of them kills a pod that is only
    # reporting on a dependency it already tolerates.
    #
    # `jobs` (db3, register item 50) is reported the same way: a dead jobs
    # store 503s call pushes and polls, but notes are still judged, so the pod
    # stays in rotation and says which half is degraded.
    cost_ready, operational_ready, jobs_ready = await asyncio.gather(
        check_cost_redis_ready(),
        check_operational_redis_ready(),
        check_jobs_redis_ready(),
    )
    return {
        "status": "ready",
        "llm": "ok",
        "redis": "ok" if cost_ready else "degraded",
        "operational": "ok" if operational_ready else "degraded",
        "jobs": "ok" if jobs_ready else "degraded",
    }


async def metrics() -> Response:
    """The Prometheus page (register item 22), or 404 unless
    DODEAL_METRICS_ENABLED. No gate: it carries no tenant and no note."""
    if not get_settings().metrics_enabled:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    # The call queues' depth is read now, not tracked (register item 54).
    await refresh_queue_depths()
    return Response(service_metrics.render(), media_type=service_metrics.CONTENT_TYPE)


def create_app() -> FastAPI:
    """The service app: lifespan, three middlewares, the judgement routes,
    /health and /ready. The gate-chain probe is NOT mounted (register item 93);
    a test that needs it mounts api/routes/_probe.py on its own app."""
    application = FastAPI(title="DODEAL AI Intelligence Layer", lifespan=lifespan)

    # MIDDLEWARE ORDER, and it is the reverse of how it reads. `add_middleware`
    # INSERTS AT THE FRONT of the list, and the front of that list is the
    # OUTERMOST layer -- so the LAST call below is the first middleware a
    # request meets. Registered inflight, then request-id, then body-limit gives
    # a request the order: BODY LIMIT, then REQUEST ID, then INFLIGHT, then
    # routing and the gates. Each is cheaper than the one under it.
    #
    # Load shedding, innermost of the two and before routing, the gate chain and
    # any body read: a refusal must cost a counter comparison and nothing more.
    application.add_middleware(InflightMiddleware)

    # Starlette always wraps user middleware inside its own outermost
    # ServerErrorMiddleware, so this runs inside that fail-closed boundary but
    # before routing/the gate chain -- see request_id.py's module docstring.
    application.add_middleware(RequestIDMiddleware)

    # OUTERMOST of the three, and this must stay the LAST add_middleware call:
    # bytes are refused before an id is minted, before a slot is taken and
    # before any gate runs (register item 87). Its 413 therefore carries
    # request_id "unknown" on the header path, the cost of refusing that early.
    application.add_middleware(BodyLimitMiddleware)

    application.include_router(judgements.router)
    application.include_router(admin.router)
    application.include_router(measures.router)
    application.include_router(calls.router)
    application.add_api_route("/health", health, methods=["GET"])
    register_error_handlers(application)
    application.add_api_route("/ready", ready, methods=["GET"])
    application.add_api_route(
        "/metrics", metrics, methods=["GET"], include_in_schema=False
    )
    return application


app = create_app()
