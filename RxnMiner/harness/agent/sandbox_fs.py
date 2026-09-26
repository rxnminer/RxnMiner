"""Host-side sandbox workspace for High-Precision mode.

The agent gets a real directory it OWNS (one per job) to store crops, RDKit renders, and
intermediate notes, referenced BY PATH instead of stuffed into context. This is the user's
core ask ("store images in the sandbox, not in memory"). We run it as a plain host directory
(not a container): the agent only ever calls our FIXED tool menu -- it never executes arbitrary
code -- so container isolation buys little, and this server's rootless Docker is hostile
(broken exec/attach, ~7s/verb). The `SandboxFS` abstraction keeps Docker swappable later.

Parallelism: each job's ws nests under its unique `JOBS_DIR/<job_id>/work/sandbox_ws/`, so
concurrent jobs never collide. Every FS verb is path-guarded to the workspace; `read_skill`
is allowlisted to the read-only skill methodology dir.
"""
from __future__ import annotations

import os
from pathlib import Path

_MAX_READ = 20000      # cap a read_file payload (chars)
_MAX_SKILL = 8000      # cap a read_skill payload (chars) -- progressive disclosure, one chunk at a time


class SandboxFS:
    def __init__(self, ws_dir, skill_dir):
        self.ws = Path(ws_dir).resolve()
        self.ws.mkdir(parents=True, exist_ok=True)
        self.skill = Path(skill_dir).resolve()

    # -- path safety -------------------------------------------------------
    def _safe(self, path, base=None):
        """Resolve `path` under `base` (default = workspace); reject anything that escapes it."""
        b = (Path(base) if base else self.ws).resolve()
        p = Path(path)
        rp = (b / p).resolve() if not p.is_absolute() else p.resolve()
        if not (rp == b or str(rp).startswith(str(b) + os.sep)):
            raise ValueError(f"path escapes sandbox: {path!r}")
        return rp

    def abspath(self, name):
        """Absolute workspace path for a ws-relative name (for tools that write into the ws)."""
        return str(self._safe(name))

    # -- FS verbs (each returns a json-able dict / str) --------------------
    def ls(self, path="."):
        try:
            t = self._safe(path)
        except ValueError as e:
            return {"error": str(e)}
        if not t.exists():
            return {"error": "not found", "path": str(t)}
        if t.is_file():
            return {"file": str(t), "bytes": t.stat().st_size}
        return {"dir": str(t),
                "entries": sorted(e.name + ("/" if e.is_dir() else "") for e in t.iterdir())}

    def read_file(self, path):
        try:
            p = self._safe(path)
        except ValueError as e:
            return {"error": str(e)}
        if not p.is_file():
            return {"error": "not a file", "path": str(p)}
        return p.read_text(errors="replace")[:_MAX_READ]

    def write_note(self, path, text=""):
        try:
            p = self._safe(path)
        except ValueError as e:
            return {"error": str(e)}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text or "")
        return {"wrote": str(p), "bytes": p.stat().st_size}

    def read_skill(self, path):
        """Progressive disclosure: read one methodology file from the read-only skill dir.
        ANTI-LEAKAGE: examples/ (paper23/49/50 worked walkthroughs + gt_audit) carry the GT answers, so they
        are NEVER readable via read_skill -- only the methodology .md (WORKFLOW/PITFALLS/SCHEMA/CASES/...)."""
        try:
            p = self._safe(path, base=self.skill)
        except ValueError as e:
            return {"error": str(e)}
        if "examples" in p.relative_to(self.skill).parts:
            return {"error": "examples/ is blocked (anti-leakage) — read the methodology files "
                    "(WORKFLOW.md / PITFALLS.md / SCHEMA.md / CASES.md / RDKIT_RECIPES.md / TOOLS.md) instead"}
        if not p.is_file():
            return {"error": "not found under skill/", "path": str(path)}
        return p.read_text(errors="replace")[:_MAX_SKILL]
