"""Shared paths for molecular_assembly (src / web / batch)."""
from __future__ import annotations

import sys
from pathlib import Path

MA_ROOT = Path(__file__).resolve().parent
SRC_DIR = MA_ROOT / "src"
DATA_DIR = MA_ROOT / "data"
# Official Synfacts corpus (tests / reverse assembly / decompose)
JSON_DIR = DATA_DIR / "json"
IMAGE_DIR = DATA_DIR / "image"
WEB_DIR = MA_ROOT / "web"
WEB_SERVERS_DIR = WEB_DIR / "servers"
WEB_LAUNCH_DIR = WEB_DIR / "launch"
VIEWER_DIR = WEB_SERVERS_DIR  # backward compat alias
BATCH_DIR = MA_ROOT / "batch"
ARCHIVE_DIR = MA_ROOT / "_archive"
NO_BATCH_JSON = ARCHIVE_DIR / "debug" / "_no_batch.json"


def setup_src_path() -> Path:
    """Put repo root + ``src/`` on sys.path.

    - ``src/`` so ``import decompose`` etc. work from web/batch.
    - repo root so ``from batch.batch_decompose import …`` works as a package.
    """
    root = str(MA_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    s = str(SRC_DIR)
    if s not in sys.path:
        sys.path.insert(0, s)
    return SRC_DIR
