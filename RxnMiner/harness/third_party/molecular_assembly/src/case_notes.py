"""Append-only JSONL log for per-JSON case remarks (SMILES edits, Step0 runs)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ma_paths import BATCH_DIR

DEFAULT_CASE_NOTES = BATCH_DIR / "logs" / "case_notes.jsonl"
_CASE_KEY_RE = re.compile(r"^\d+:\d+$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_case_key(case_key: str) -> str:
    key = (case_key or "").strip()
    if not _CASE_KEY_RE.match(key):
        raise ValueError("case_key must look like example_index:product_index")
    return key


def validate_json_file(json_file: str) -> str:
    base = Path(json_file).name
    if base != json_file or ".." in base:
        raise ValueError("invalid json_file")
    if not base.endswith("-with-examples.json"):
        raise ValueError("json_file must end with -with-examples.json")
    return base


def append_case_note(
    *,
    json_file: str,
    case_key: str,
    event: str,
    note: str = "",
    snapshot: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    step0: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one JSONL record; return the stored entry."""
    jf = validate_json_file(json_file)
    ck = validate_case_key(case_key)
    ev = (event or "note").strip() or "note"
    entry: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "json_file": jf,
        "case_key": ck,
        "event": ev,
        "note": (note or "").strip(),
        "snapshot": snapshot or {},
    }
    if diff:
        entry["diff"] = diff
    if step0:
        entry["step0"] = step0

    p = path or DEFAULT_CASE_NOTES
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_case_notes(
    *,
    json_file: str | None = None,
    case_key: str | None = None,
    limit: int = 20,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return newest-first notes; optional filter by json_file and/or case_key."""
    p = path or DEFAULT_CASE_NOTES
    if not p.is_file():
        return []
    lim = max(1, min(int(limit), 200))
    jf_filter = validate_json_file(json_file) if json_file else None
    ck_filter = validate_case_key(case_key) if case_key else None

    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if jf_filter and row.get("json_file") != jf_filter:
                continue
            if ck_filter and row.get("case_key") != ck_filter:
                continue
            rows.append(row)
    rows.reverse()
    return rows[:lim]
