"""Verify that a built wheel installs and imports outside the repo (audit F1).

The wheel used to omit schemas/ and prompts/, so an installed copy failed with
ModuleNotFoundError the moment tools/leads.py was imported, and core/prompting
could not find its templates. This only worked in the repo because pytest sets
pythonpath = ["."] and the local install is editable — neither is true for a
real deploy.

This script installs the given wheel into a throwaway venv, then runs the
import check from a temp working directory that is NOT the repo, so nothing
on disk here (the editable install, the src/ layout) can make a broken wheel
look like it works.

Usage:
    python scripts/verify_wheel.py <path-to-wheel>

stdlib only — this must run before dev dependencies are known to be sound.

The required CI checks, in order (.github/workflows/ci.yml, the main job).
All seven must be green before a branch merges -- see CONTRIBUTING, "Branch
protection":

    1. uv run ruff check .
    2. uv run ruff format --check .
    3. uv run mypy
    4. uv run lint-imports                        (the import layers)
    5. uv run pytest                              (total coverage floor: 92%)
    6. uv run python scripts/check_coverage_floors.py   (per-FILE floors)
    7. uv build --wheel  +  this script            (installs and imports)

This script is step 7: it is the last gate, and the only one that tests the
artifact a deploy actually receives rather than the source tree. The dependency
audit and the real-Redis and load lanes run as jobs of their own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

_IMPORT_CHECK = """
import importlib
import pkgutil
import sys

import dodeal_ai

failures = []
for module_info in pkgutil.walk_packages(dodeal_ai.__path__, "dodeal_ai."):
    try:
        importlib.import_module(module_info.name)
    except Exception as exc:
        failures.append((module_info.name, exc))

if failures:
    for name, exc in failures:
        print(f"FAILED to import {name}: {exc!r}", file=sys.stderr)
    raise SystemExit(1)

from dodeal_ai.core import prompting

assert prompting._DEFAULT_PROMPTS_DIR.is_dir(), (
    f"prompts dir missing from installed package: {prompting._DEFAULT_PROMPTS_DIR}"
)
assert (prompting._DEFAULT_PROMPTS_DIR / "unit_a_v1.txt").is_file(), (
    "unit_a_v1.txt missing from installed package"
)

print("wheel import check: OK")
"""


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_wheel.py <path-to-wheel>", file=sys.stderr)
        return 2

    wheel_path = Path(sys.argv[1]).resolve()
    if not wheel_path.is_file():
        print(f"wheel not found: {wheel_path}", file=sys.stderr)
        return 2

    with (
        tempfile.TemporaryDirectory(prefix="dodeal_verify_venv_") as venv_dir_str,
        tempfile.TemporaryDirectory(prefix="dodeal_verify_run_") as run_dir_str,
    ):
        venv_dir = Path(venv_dir_str)
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        python = _venv_python(venv_dir)

        install = subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", str(wheel_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            print("pip install failed:", file=sys.stderr)
            print(install.stdout, file=sys.stderr)
            print(install.stderr, file=sys.stderr)
            return install.returncode

        check = subprocess.run(
            [str(python), "-c", _IMPORT_CHECK],
            cwd=run_dir_str,  # NOT the repo -- proves nothing depends on the source tree
            capture_output=True,
            text=True,
            env={**os.environ, "DODEAL_JWT_SIGNING_KEY": "verify-wheel-dummy-key"},
            check=False,
        )
        sys.stdout.write(check.stdout)
        if check.returncode != 0:
            sys.stderr.write(check.stderr)
            return check.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
