"""The console's file-and-callback server: every uploaded recording to fetch,
and a stand-in CRM to call back, on one loopback port.

  GET  /audio/<name>   a recording the console saved in --dir, by the random
                       name it gave it (32 hex characters and a suffix); any
                       other name is 404, so nothing else on the disk is served
  POST /callback       call_demo.py's receiver: the signature is checked with
                       the run's secret and the event and the four X-DODEAL
                       headers are printed -- NEVER the body, which carries the
                       transcript

One process for every upload, where call_e2e.py starts one per recording: the
console pushes calls for as long as it runs. Loopback only.

Usage (the console starts it):
    python -m scripts.console.files --port <p> --secret <s> --dir <folder>
"""

from __future__ import annotations

import argparse
import mimetypes
import re
from collections.abc import Callable
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from scripts.call_demo import _HEADERS, CALLBACK_PATH, verified

AUDIO_PREFIX = "/audio/"
# The only names served: what stack.push_call names an upload, 32 hex
# characters and the recording's suffix.
_SERVED_NAME = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,5}$")
# What the worker's download accepts (core/audio_download.py, rule 7): audio/*
# or untyped bytes for ffprobe to judge. A video/* type would be refused.
_UNTYPED = "application/octet-stream"


def media_type(name: str) -> str:
    """audio/* by the name's suffix, else untyped bytes."""
    guessed, _ = mimetypes.guess_type(name)
    return guessed if guessed and guessed.startswith("audio/") else _UNTYPED


def build_app(
    secret: str, folder: Path, emit: Callable[[str], None] = print
) -> Starlette:
    """The recordings in `folder` and the stand-in CRM, on one app."""
    root = folder.resolve()

    async def recording(request: Request) -> Response:
        name = str(request.path_params["name"])
        path = root / name
        if not _SERVED_NAME.match(name) or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type=media_type(name))

    async def callback(request: Request) -> Response:
        body = await request.body()
        headers = {name: request.headers.get(name, "") for name in _HEADERS}
        ok = verified(
            secret, headers["X-DODEAL-Timestamp"], body, headers["X-DODEAL-Signature"]
        )
        status = 204 if ok else 401
        emit(f"callback {headers['X-DODEAL-Event'] or '(no event)'}")
        for name in _HEADERS:
            emit(f"  {name}: {headers[name]}")
        emit(f"  signature: {'verified' if ok else 'REFUSED'}  status: {status}")
        return Response(status_code=status)

    return Starlette(
        routes=[
            Route(AUDIO_PREFIX + "{name}", recording, methods=["GET"]),
            Route(CALLBACK_PATH, callback, methods=["POST"]),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)
    app = build_app(args.secret, args.dir, emit=lambda line: print(line, flush=True))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
