from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from dodeal_ai.api.routes import _probe
from dodeal_ai.core.config import ConfigError, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed: if required config (signing key) is absent, refuse to start.
    get_settings()
    yield


app = FastAPI(title="DODEAL AI Intelligence Layer", lifespan=lifespan)

app.include_router(_probe.router)
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready():
    try:
        get_settings()
    except ConfigError:
        return JSONResponse(status_code=503, content={"status": "not ready"})
    return {"status": "ready"}