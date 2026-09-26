"""Batch Step0 summary CSV: ``jsonname,total,success,roundtrip_success``.

``success`` = cases with at least one full-pass candidate (bond + peel topology).
``roundtrip_success`` = cases with at least one roundtrip-pass candidate.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ma_paths import BATCH_DIR

CSV_FIELDS = ("jsonname", "total", "success", "roundtrip_success")
DEFAULT_BATCH_SUMMARY = BATCH_DIR / "logs" / "decompose_summary.csv"
# Legacy TSV log (read-only fallback for old runs)
DEFAULT_BATCH_LOG = BATCH_DIR / "logs" / "decompose.log"


def task_name_from_json_file(filename: str) -> str:
    """Short label (no suffix); used for search hints only."""
    if filename.endswith("-with-examples.json"):
        return filename[: -len("-with-examples.json")]
    return Path(filename).stem


def init_summary_csv(path: Path | None = None) -> Path:
    p = path or DEFAULT_BATCH_SUMMARY
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
    return p


def read_summary_csv(path: Path | None = None) -> list[dict[str, Any]]:
    """Rows as ``{jsonname, total, success}``; later rows override same jsonname."""
    p = path or DEFAULT_BATCH_SUMMARY
    if not p.is_file():
        return _read_legacy_log_as_summary()
    by_name: dict[str, dict[str, Any]] = {}
    with p.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("jsonname") or "").strip()
            if not name:
                continue
            try:
                total = int(row.get("total") or 0)
                success = int(row.get("success") or 0)
                roundtrip_success = int(row.get("roundtrip_success") or 0)
            except ValueError:
                continue
            by_name[name] = {
                "jsonname": name,
                "total": total,
                "success": success,
                "roundtrip_success": roundtrip_success,
            }
    return list(by_name.values())


def _read_legacy_log_as_summary() -> list[dict[str, Any]]:
    """Map old ``decompose.log`` tab lines to CSV-shaped rows (short task → full jsonname)."""
    log_path = DEFAULT_BATCH_LOG
    if not log_path.is_file():
        return []
    out: dict[str, dict[str, Any]] = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("task"):
            continue
        parts = line.split("\t")
        task = parts[0].strip()
        if not task:
            continue
        jsonname = (
            task
            if task.endswith(".json")
            else f"{task}-with-examples.json"
        )
        if len(parts) >= 2 and parts[1].strip().upper() == "ERROR":
            out[jsonname] = {
                "jsonname": jsonname,
                "total": 0,
                "success": 0,
                "roundtrip_success": 0,
            }
            continue
        try:
            if len(parts) >= 3:
                total = int(parts[1].strip())
                success = int(parts[2].strip())
            else:
                total = 0
                success = int(parts[1].strip())
        except (ValueError, IndexError):
            continue
        out[jsonname] = {
            "jsonname": jsonname,
            "total": total,
            "success": success,
            "roundtrip_success": 0,
        }
    return list(out.values())


def load_done_jsonnames(path: Path | None = None) -> set[str]:
    return {e["jsonname"] for e in read_summary_csv(path)}


def upsert_summary_row(
    jsonname: str,
    total: int,
    success: int,
    path: Path | None = None,
    *,
    roundtrip_success: int = 0,
) -> None:
    """Insert or replace one JSON row and rewrite the CSV."""
    p = path or DEFAULT_BATCH_SUMMARY
    rows = read_summary_csv(p) if p.is_file() else []
    by_name = {r["jsonname"]: r for r in rows}
    by_name[jsonname] = {
        "jsonname": jsonname,
        "total": max(0, int(total)),
        "success": max(0, int(success)),
        "roundtrip_success": max(0, int(roundtrip_success)),
    }
    ordered = sorted(by_name.values(), key=lambda r: r["jsonname"])
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in ordered:
            w.writerow(
                {
                    "jsonname": r["jsonname"],
                    "total": r["total"],
                    "success": r["success"],
                    "roundtrip_success": r.get("roundtrip_success", 0),
                }
            )


def fail_json_names(path: Path | None = None) -> list[str]:
    """JSON files with ``success == 0`` (no case had a full-pass candidate)."""
    return sorted(
        e["jsonname"]
        for e in read_summary_csv(path)
        if int(e.get("success") or 0) == 0
    )


# --- API compat (old names) ---


def parse_batch_log(path: Path | None = None) -> list[dict[str, Any]]:
    entries = read_summary_csv(path or DEFAULT_BATCH_SUMMARY)
    return [
        {
            "task": task_name_from_json_file(e["jsonname"]),
            "jsonname": e["jsonname"],
            "total": e["total"],
            "success": e["success"],
            "roundtrip_success": int(e.get("roundtrip_success") or 0),
            "error": None,
        }
        for e in entries
    ]


def fail_task_names(path: Path | None = None) -> list[str]:
    """Short task names for fail-only filter (matches legacy UI search)."""
    return [task_name_from_json_file(n) for n in fail_json_names(path)]


def summary_stats(path: Path | None = None) -> dict[str, int]:
    entries = read_summary_csv(path)
    case_total = sum(int(e["total"]) for e in entries)
    case_success = sum(int(e["success"]) for e in entries)
    case_roundtrip_success = sum(int(e.get("roundtrip_success") or 0) for e in entries)
    return {
        "entry_count": len(entries),
        "case_total": case_total,
        "case_success": case_success,
        "case_roundtrip_success": case_roundtrip_success,
        "fail_zero_count": sum(1 for e in entries if int(e["success"]) == 0),
    }
