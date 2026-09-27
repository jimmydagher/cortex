#!/usr/bin/env python3
"""Run every local check the way CI runs it: lint, type-check, tests.

    python scripts/python/check.py            # dev machine, repo root, inside .venv
    python scripts/python/check.py pytest     # one check by name

The tools' settings live here, as their command-line flags, so the repo needs no extra
config files. Exit 0 when every check passes, 1 when any fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Vendored from the SDSI plugin and copied unchanged (sdsi:versioning): not checked as project code.
VENDORED = "scripts/python/release.py"
TYPED_SCRIPTS = ["scripts/python/check.py", "scripts/python/check_changelog.py",
                 "scripts/python/lock.py", "scripts/python/make_favicon.py"]

CHECKS: dict[str, list[str]] = {
    "ruff": [
        "ruff", "check", ".", "--target-version", "py314", "--line-length", "120",
        "--select", "E,F,W,I,B,UP,N,SIM,BLE",
        # Tool descriptions, log formats and error messages read better on one line than wrapped.
        "--ignore", "E501",
        "--extend-exclude", VENDORED,
    ],
    "mypy": [
        "mypy", "--strict", "--python-version", "3.14",
        # Imported but not listed (the vendored release.py): followed for types, errors not reported.
        "--follow-imports", "silent",
        "src", "tests", *TYPED_SCRIPTS,
    ],
    "pytest": ["pytest", "tests", "-q"],
}


def run(name: str) -> bool:
    """Run one check and report it.

    Args:
        name: a key of CHECKS.

    Returns:
        True when the check passed.
    """
    print(f"== {name}", flush=True)
    environment = {**os.environ, "MYPYPATH": os.pathsep.join(["src", "tests", "scripts/python"])}
    command = [sys.executable, "-m", *CHECKS[name]]
    return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode == 0


def main() -> int:
    """Run the named checks, or all of them.

    Returns:
        0 when every check passed, 1 otherwise.
    """
    names = sys.argv[1:] or list(CHECKS)
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}; choose from {', '.join(CHECKS)}", file=sys.stderr)
        return 1
    failed = [name for name in names if not run(name)]
    print(f"FAILED: {', '.join(failed)}" if failed else "all checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
