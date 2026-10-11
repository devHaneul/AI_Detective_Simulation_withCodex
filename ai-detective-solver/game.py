#!/usr/bin/env python3
"""Solver-safe entry point for the game controller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SOLVER_DIR = Path(__file__).resolve().parent
ROOT = SOLVER_DIR.parent


def main() -> int:
    command = [sys.executable, str(ROOT / "play.py"), *sys.argv[1:]]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
