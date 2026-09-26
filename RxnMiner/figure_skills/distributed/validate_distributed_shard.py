#!/usr/bin/env python3
"""Fail-closed validator for blind shard inputs and returned extraction artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

FORBIDDEN_NAMES = re.compile(r"(^|[._-])(gt|ground.?truth|reference|accepted|reviewed|answer|score)([._-]|$)", re.I)
FORBIDDEN_KEYS = {"gt", "ground_truth", "reference", "accepted", "reviewed", "answer", "score"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def forbidden_key(value) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                return str(key)
            found = forbidden_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = forbidden_key(child)
            if found:
                return found
    return None


def read_manifest(shard: Path) -> list[dict]:
    path = shard / "manifest.pre_reference.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row.get("id") for row in rows]
    if not rows or None in ids or len(ids) != len(set(ids)):
        raise ValueError("manifest is empty or has invalid/duplicate ids")
    for row in rows:
        bad = forbidden_key(row)
        if bad:
            raise ValueError(f"manifest contains forbidden key: {bad}")
    return rows


def validate_input(shard: Path) -> tuple[list[dict], list[str]]:
    errors = []
    try:
        rows = read_manifest(shard)
    except Exception as exc:
        return [], [str(exc)]
    allowed_root = {
        "manifest.pre_reference.jsonl", "SHARD_METADATA.json", "RUN_CODEX.md", "run.sh",
        "input", "PREFLIGHT.json", "results",
    }
    extra_root = sorted(
        path.name for path in shard.iterdir()
        if path.name not in allowed_root
        and not path.name.endswith(".submission.tar.gz")
        and not path.name.endswith(".submission.tar.gz.sha256")
    )
    if extra_root:
        errors.append(f"unexpected shard-root entries: {extra_root}")
    for path in (shard / "input").rglob("*"):
        if path.is_file() and FORBIDDEN_NAMES.search(path.name):
            errors.append(f"forbidden input filename: {path.name}")
    for row in rows:
        image = shard / row["image"]
        cache = shard / row["cache"]
        if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
            errors.append(f"{row['id']}: missing image")
        elif sha256(image) != row.get("image_sha256"):
            errors.append(f"{row['id']}: image hash mismatch")
        if not cache.is_file():
            errors.append(f"{row['id']}: missing cache")
        elif sha256(cache) != row.get("cache_sha256"):
            errors.append(f"{row['id']}: cache hash mismatch")
        else:
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
                if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                    errors.append(f"{row['id']}: invalid cache payload")
            except Exception:
                errors.append(f"{row['id']}: unreadable cache payload")
    return rows, errors


def validate_output(shard: Path, rows: list[dict], result: Path) -> list[str]:
    errors = []
    if not result.is_dir():
        return [f"result directory does not exist: {result}"]
    for row in rows:
        image = shard / row["image"]
        digest = row["image_sha256"][:12]
        stem = image.stem
        expected_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{stem}-{digest}").strip("_")
        case = result / "cases" / expected_prefix
        if not case.is_dir():
            errors.append(f"{row['id']}: missing output case {expected_prefix}")
            continue
        output = case / "output"
        for name in ("pred.v2.json", "RUN_METADATA.json", "SEAL.pre_reference.json"):
            if not (output / name).is_file():
                errors.append(f"{row['id']}: missing {name}")
        metadata = output / "RUN_METADATA.json"
        if metadata.is_file():
            try:
                payload = json.loads(metadata.read_text(encoding="utf-8"))
                text = json.dumps(payload).lower()
                if any(marker in text for marker in ("gt.json", "reviewed.json", "accepted.v2")):
                    errors.append(f"{row['id']}: forbidden reference marker in metadata")
            except Exception:
                errors.append(f"{row['id']}: invalid RUN_METADATA.json")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    rows, errors = validate_input(args.shard)
    if args.result and not errors:
        errors.extend(validate_output(args.shard, rows, args.result))
    report = {"schema": "chemrxn-distributed-validation-v1", "shard": str(args.shard), "count": len(rows), "valid": not errors, "errors": errors}
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(text)
    print(text, end="")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
