import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from dodeal_ai.api.routes import _probe, judgements
from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.core.errors import register_error_handlers
from dodeal_ai.core.logging_config import configure_logging
from dodeal_ai.core.redis import (
    check_cost_redis_ready,
    get_cost_client,
    get_operational_client,
)
from dodeal_ai.middleware.inflight import InflightMiddleware
from dodeal_ai.middleware.request_id import RequestIDMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed: if required config (signing key) is absent, refuse to start.
    settings = get_settings()
    configure_logging()
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
        logging.getLogger("dodeal_ai.startup").error("backend_keys_missing count=0")
    yield
    # Release the connection pools on shutdown. from_url opens no socket, so
    # constructing a client here only to close it costs nothing, and closing
    # unconditionally keeps shutdown symmetrical whether or not the app ever
    # used it. The queue connection is arq's, closed by arq.
    await get_cost_client().aclose()
    await get_operational_client().aclose()


app = FastAPI(title="DODEAL AI Intelligence Layer", lifespan=lifespan)

# MIDDLEWARE ORDER, and it is the reverse of how it reads. `add_middleware`
# INSERTS AT THE FRONT of the list, and the front of that list is the OUTERMOST
# layer -- so the LAST call below is the first middleware a request meets.
# Registered inflight-then-request-id gives request-id OUTSIDE inflight, which
# is the order that is wanted: a refused request still gets an id.
#
# Load shedding, innermost of the two and before routing, the gate chain and
# any body read: a refusal must cost a counter comparison and nothing more.
app.add_middleware(InflightMiddleware)

# Starlette always wraps user middleware inside its own outermost
# ServerErrorMiddleware, so this runs inside that fail-closed boundary but
# before routing/the gate chain -- see request_id.py's module docstring.
app.add_middleware(RequestIDMiddleware)

app.include_router(_probe.router)
app.include_router(judgements.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# register_body_size_limit(app)
register_error_handlers(app)


@app.get("/ready")
async def ready():
    try:
        get_settings()
    except ConfigError:
        return JSONResponse(status_code=503, content={"status": "not ready"})
    # Redis down does NOT take the pod out of rotation: the cost gate fails
    # OPEN on a Redis outage (core/cost/limiter.py), so the service still
    # serves requests correctly without it. Report the degraded state in the
    # body so it's observable, but keep 200 so an orchestrator doesn't pull a
    # functioning pod over a non-critical dependency.
    if await check_cost_redis_ready():
        return {"status": "ready", "redis": "ok"}
    return {"status": "ready", "redis": "degraded"}
