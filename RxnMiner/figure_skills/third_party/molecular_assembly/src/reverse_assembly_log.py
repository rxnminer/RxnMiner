"""CSV + JSON cache for reverse-assembly batch runs."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ma_paths import BATCH_DIR

REVERSE_SUMMARY_FIELDS = (
    "image_name",
    "page_index",
    "example_index",
    "instance_id",
    "yield_text",
    "included",
    "skip_reason",
    "assembly_ok",
    "compare_ok",
    "stereo_warn",
    "status_label",
    "failure_category",
    "detail",
    "notes",
)
DEFAULT_REVERSE_SUMMARY = BATCH_DIR / "logs" / "reverse_assembly_summary.csv"
DEFAULT_REVERSE_CACHE_DIR = BATCH_DIR / "cache" / "reverse"
CACHE_VERSION = 1


@dataclass
class ReverseExampleSummaryRow:
    image_name: str = ""
    page_index: int = 0
    example_index: int = 0
    instance_id: str = ""
    yield_text: str = ""
    included: bool = True
    skip_reason: str = ""
    assembly_ok: bool | None = None
    compare_ok: bool | None = None
    stereo_warn: bool = False
    status_label: str = ""
    failure_category: str = ""
    detail: str = ""
    notes: str = ""

    def csv_dict(self) -> dict[str, str]:
        d = asdict(self)
        d["included"] = "1" if self.included else "0"
        d["stereo_warn"] = "1" if self.stereo_warn else "0"
        for key in ("assembly_ok", "compare_ok"):
            val = d[key]
            d[key] = "" if val is None else ("1" if val else "0")
        return {k: str(d[k]) for k in REVERSE_SUMMARY_FIELDS}

    @classmethod
    def from_csv_dict(cls, row: dict[str, str]) -> ReverseExampleSummaryRow:
        def _bool_opt(key: str) -> bool | None:
            raw = (row.get(key) or "").strip()
            if raw == "":
                return None
            return raw in ("1", "true", "True", "yes")

        return cls(
            image_name=row.get("image_name") or "",
            page_index=int(row.get("page_index") or 0),
            example_index=int(row.get("example_index") or 0),
            instance_id=row.get("instance_id") or "",
            yield_text=row.get("yield_text") or "",
            included=(row.get("included") or "1").strip() in ("1", "true", "True"),
            skip_reason=row.get("skip_reason") or "",
            assembly_ok=_bool_opt("assembly_ok"),
            compare_ok=_bool_opt("compare_ok"),
            stereo_warn=(row.get("stereo_warn") or "").strip() in ("1", "true", "True"),
            status_label=row.get("status_label") or "",
            failure_category=row.get("failure_category") or "",
            detail=row.get("detail") or "",
            notes=row.get("notes") or "",
        )


def init_reverse_summary_csv(path: Path | None = None) -> Path:
    p = path or DEFAULT_REVERSE_SUMMARY
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=REVERSE_SUMMARY_FIELDS).writeheader()
    return p


def read_reverse_summary_csv(path: Path | None = None) -> list[ReverseExampleSummaryRow]:
    p = path or DEFAULT_REVERSE_SUMMARY
    if not p.is_file():
        return []
    rows: list[ReverseExampleSummaryRow] = []
    with p.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(ReverseExampleSummaryRow.from_csv_dict(row))
    return rows


def load_done_reverse_keys(path: Path | None = None) -> set[tuple[int, int]]:
    """``(page_index, example_index)`` pairs already in CSV."""
    return {(r.page_index, r.example_index) for r in read_reverse_summary_csv(path)}


def upsert_reverse_summary_rows(
    rows: list[ReverseExampleSummaryRow],
    path: Path | None = None,
) -> None:
    p = path or DEFAULT_REVERSE_SUMMARY
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {(r.page_index, r.example_index): r for r in read_reverse_summary_csv(p)}
    for row in rows:
        existing[(row.page_index, row.example_index)] = row
    ordered = sorted(
        existing.values(),
        key=lambda r: (r.page_index, r.example_index),
    )
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REVERSE_SUMMARY_FIELDS)
        w.writeheader()
        for row in ordered:
            w.writerow(row.csv_dict())


def reverse_cache_path(
    page_index: int,
    example_index: int,
    *,
    cache_dir: Path | None = None,
) -> Path:
    base = cache_dir or DEFAULT_REVERSE_CACHE_DIR
    return base / f"p{page_index}_e{example_index}.json"


def write_reverse_cache(
    page_index: int,
    example_index: int,
    payload: dict[str, Any],
    *,
    cache_dir: Path | None = None,
) -> Path:
    path = reverse_cache_path(page_index, example_index, cache_dir=cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def read_reverse_cache(
    page_index: int,
    example_index: int,
    *,
    cache_dir: Path | None = None,
) -> dict[str, Any] | None:
    path = reverse_cache_path(page_index, example_index, cache_dir=cache_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("version") != CACHE_VERSION:
        return None
    return data


def aggregate_failure_counts(rows: list[ReverseExampleSummaryRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if not row.included:
            key = "skipped"
        elif row.status_label == "一致":
            key = "ok"
        else:
            key = row.failure_category or row.status_label or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
