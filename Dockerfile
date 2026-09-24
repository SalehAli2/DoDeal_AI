FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
WORKDIR /app
# Register item 94: the service never runs as root. A fixed numeric id, so an
# orchestrator's runAsNonRoot check can verify it without reading /etc/passwd.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app
# The call workers' audio layer (units/call_intelligence/audio.py): a worker
# checks ffmpeg at start and refuses without it. From the distribution, so it
# is patched with the base image; no recommends, and no package lists left.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
EXPOSE 8000
# /health answers without Redis, a model or a backend; stdlib only, no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
# uvicorn with --proxy-headers, --forwarded-allow-ips from DODEAL_FORWARDED_ALLOW_IPS
# and a 30 s graceful shutdown on SIGTERM (src/dodeal_ai/serve.py).
STOPSIGNAL SIGTERM
CMD ["python", "-m", "dodeal_ai.serve"]
