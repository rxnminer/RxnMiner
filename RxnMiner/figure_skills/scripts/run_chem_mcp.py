#!/usr/bin/env python3
"""Start chem-extract, bootstrapping a private Python environment when needed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "tools" / "chem_mcp.py"
REQUIREMENTS = ROOT / "requirements.txt"


def _runtime_ready(python: str) -> bool:
    probe = "import PIL, rdkit, requests"
    try:
        return subprocess.run(
            [python, "-c", probe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def _exec(python: str) -> None:
    os.chdir(ROOT)
    os.execv(python, [python, str(SERVER)])


if _runtime_ready(sys.executable):
    _exec(sys.executable)

cache_root = Path(
    os.environ.get(
        "RXN_SKILLS_CACHE_DIR",
        Path.home() / ".cache" / "rxn-skills",
    )
).expanduser()
venv = cache_root / "venv"
python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

if not _runtime_ready(str(python)):
    cache_root.mkdir(parents=True, exist_ok=True)
    if not python.exists():
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
    subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )

if not _runtime_ready(str(python)):
    raise SystemExit("rxn-skills runtime bootstrap completed but required imports still fail")

_exec(str(python))
