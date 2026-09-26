"""engine.perceive — the OCSR (optical chemical structure recognition) client.
This is the MolParser HTTP client, verbatim from the repo (agent/tools.py). It posts the
figure image to a remote MolParser endpoint and returns one detection per drawn structure.

REMOTE DEPENDENCY: MolParser runs at MOLPARSER_URL (a CN-hosted endpoint). It needs
network and must NOT go through a SOCKS proxy (the client strips proxy env vars per call).
Each detection dict carries the recognised SMILES under 'caption' (our convention:
caption > smi), a 'bbox' [x0,y0,x1,y1] in figure pixels, and a 'markush' flag.

tool.py normalises a detection to {idx, smiles, bbox, markush, score}.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

# The live endpoint (CN-only). Override with $MOLPARSER_URL if you tunnel/relocate it.
PROXY_KEYS = ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]


def molparser(image_path, timeout=180):
    """Recognize a source image using the configured UniParser account."""
    import sys
    source_root = str(Path(__file__).resolve().parents[4])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from figure_skills.tools.uniparser_client import recognize_molecules
    return recognize_molecules(image_path, timeout=timeout)


def smiles_of(detection: dict) -> str:
    """Pull the SMILES from a detection (caption > smi > smiles)."""
    return (detection.get("caption") or detection.get("smi") or detection.get("smiles") or "").strip()
