from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from dodeal_ai.api.routes import _probe
from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.core.errors import register_error_handlers
from dodeal_ai.core.logging_config import configure_logging
from dodeal_ai.core.redis import check_cost_redis_ready
from dodeal_ai.middleware.request_id import RequestIDMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed: if required config (signing key) is absent, refuse to start.
    get_settings()
    configure_logging()
    yield


app = FastAPI(title="DODEAL AI Intelligence Layer", lifespan=lifespan)

# Starlette always wraps user middleware inside its own outermost
# ServerErrorMiddleware, so this runs inside that fail-closed boundary but
# before routing/the gate chain -- see request_id.py's module docstring.
app.add_middleware(RequestIDMiddleware)

app.include_router(_probe.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# register_body_size_limit(app)
register_error_handlers(app)


@app.get("/ready")
def ready():
    try:
        get_settings()
    except ConfigError:
        return JSONResponse(status_code=503, content={"status": "not ready"})
    # Redis down does NOT take the pod out of rotation: the cost gate fails
    # OPEN on a Redis outage (core/cost/limiter.py), so the service still
    # serves requests correctly without it. Report the degraded state in the
    # body so it's observable, but keep 200 so an orchestrator doesn't pull a
    # functioning pod over a non-critical dependency.
    if check_cost_redis_ready():
        return {"status": "ready", "redis": "ok"}
    return {"status": "ready", "redis": "degraded"}
