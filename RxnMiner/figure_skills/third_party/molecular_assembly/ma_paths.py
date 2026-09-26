"""Shared paths for molecular_assembly (src / web / batch)."""
from __future__ import annotations

import sys
from pathlib import Path

MA_ROOT = Path(__file__).resolve().parent
SRC_DIR = MA_ROOT / "src"
JSON_DIR = MA_ROOT / "json"
IMAGE_DIR = MA_ROOT / "image"
WEB_DIR = MA_ROOT / "web"
WEB_SERVERS_DIR = WEB_DIR / "servers"
WEB_LAUNCH_DIR = WEB_DIR / "launch"
VIEWER_DIR = WEB_SERVERS_DIR  # backward compat alias
BATCH_DIR = MA_ROOT / "batch"


def setup_src_path() -> Path:
    """Put ``src/`` on sys.path so ``import decompose`` etc. work from web/batch."""
    s = str(SRC_DIR)
    if s not in sys.path:
        sys.path.insert(0, s)
    return SRC_DIR
