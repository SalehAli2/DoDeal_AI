"""Start the local console (README, "The local console").

    uv run --group console python -m scripts.console [--dir FOLDER] [--port 8501]

Runs Streamlit on 127.0.0.1 only -- its own default is every network interface,
which would put real calls on the office network -- with its usage statistics
off, no Deploy button, uploads up to 200 MB (the service's own audio ceiling)
and app.py as the page, then opens the page in the browser. FOLDER holds every
upload, report and log: --dir, else DODEAL_CONSOLE_DIR, else ~/dodeal-console;
refused inside this repository. The stack itself is started from the page.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

from scripts.console.stack import CONSOLE_DIR_ENV, ConsoleError, console_dir

APP = Path(__file__).resolve().parent / "app.py"
ADDRESS = "127.0.0.1"
# The largest upload the page takes, in MB: the service's own ceiling on one
# recording (max_audio_bytes, 200 MiB). Lower refuses long calls the service
# would take; higher only fills the console folder with files it would refuse.
MAX_UPLOAD_MB = 200


def streamlit_argv(port: int) -> list[str]:
    """`streamlit run` for the page, bound to this machine."""
    return [
        "streamlit",
        "run",
        str(APP),
        "--server.address",
        ADDRESS,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--server.maxUploadSize",
        str(MAX_UPLOAD_MB),
        "--server.fileWatcherType",
        "none",
        "--client.toolbarMode",
        "minimal",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=os.environ.get(CONSOLE_DIR_ENV))
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = console_dir(args.dir)
    except ConsoleError as refused:
        print(f"console: {refused}")
        return 2
    os.environ[CONSOLE_DIR_ENV] = str(root)
    print(f"console folder: {root}")
    url = f"http://{ADDRESS}:{args.port}"
    if not args.no_browser:
        threading.Timer(3.0, webbrowser.open, args=(url,)).start()
    from streamlit.web import cli

    sys.argv = streamlit_argv(args.port)
    return int(cli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
