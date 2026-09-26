#!/usr/bin/env python3
"""Populate one immutable sibling MolParser cache per blind Figure case."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
from pathlib import Path

FIGURE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FIGURE_ROOT / "tools"))
sys.path.insert(0, str(FIGURE_ROOT))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_is_valid(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(payload, list) and all(isinstance(item, dict) for item in payload)
    except Exception:
        return False


def one(case: Path, force_live: bool) -> dict:
    images = [path for path in case.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}]
    if len(images) != 1:
        raise RuntimeError(f"{case.name}: expected one image, found {len(images)}")
    image = images[0]
    cache = image.with_name(f"{image.stem}.molparser.json")
    if cache_is_valid(cache) and not force_live:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        return {"id": case.name, "status": "cache_hit", "n": len(payload), "cache": cache.name, "cache_sha256": sha256(cache), "image_sha256": sha256(image)}

    # Import lazily so --help and static validation do not require chemistry dependencies.
    from chem_mcp import tool_ocsr_detect

    result = tool_ocsr_detect(str(image), force_live=force_live)
    if not cache_is_valid(cache):
        raise RuntimeError(f"{case.name}: MolParser did not persist a valid sibling cache")
    payload = json.loads(cache.read_text(encoding="utf-8"))
    provenance = result.get("ocsr") if isinstance(result, dict) else None
    return {
        "id": case.name,
        "status": "live" if provenance and not provenance.get("cache_hit") else "cache_hit",
        "n": len(payload),
        "cache": cache.name,
        "cache_sha256": sha256(cache),
        "image_sha256": sha256(image),
        "ocsr": provenance,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--force-live", action="store_true")
    args = parser.parse_args()
    cases_root = args.corpus / "cases"
    cases = sorted(path for path in cases_root.iterdir() if path.is_dir())
    if not cases:
        raise SystemExit("no cases found")

    os.environ.pop("OCSR_OFFLINE", None)
    records: list[dict] = []
    failures: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(one, case, args.force_live): case for case in cases}
        for future in concurrent.futures.as_completed(futures):
            case = futures[future]
            try:
                record = future.result()
                records.append(record)
                print(f"{record['status'].upper()} {case.name} n={record['n']}", flush=True)
            except Exception as exc:
                failures.append({"id": case.name, "error": str(exc)})
                print(f"FAIL {case.name}: {exc}", file=sys.stderr, flush=True)

    records.sort(key=lambda row: row["id"])
    failures.sort(key=lambda row: row["id"])
    ledger = args.corpus / "MOLPARSER_CACHE_LEDGER.jsonl"
    with ledger.open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema": "chemrxn-molparser-cache-ledger-v1",
        "cases": len(cases),
        "valid": len(records),
        "failed": len(failures),
        "live": sum(row["status"] == "live" for row in records),
        "cache_hit": sum(row["status"] == "cache_hit" for row in records),
        "ledger_sha256": sha256(ledger),
        "failures": failures,
    }
    (args.corpus / "MOLPARSER_CACHE_SUMMARY.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
