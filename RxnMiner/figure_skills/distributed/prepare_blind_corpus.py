#!/usr/bin/env python3
"""Stage a GT-free, hash-addressed Figure corpus from the frozen benchmark registry."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_registry(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict) or not row.get("id") or not row.get("image"):
                    raise ValueError(f"invalid registry row {len(rows) + 1}")
                rows.append(row)
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("registry contains duplicate ids")
    return rows


def stage_file(source: Path, target: Path, mode: str) -> None:
    if mode == "copy":
        shutil.copyfile(source, target)
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--mode", choices=("hardlink", "copy"), default="hardlink")
    parser.add_argument("--expected", type=int, default=1410)
    args = parser.parse_args()

    if args.out.exists():
        raise SystemExit(f"refusing to overwrite: {args.out}")
    rows = load_registry(args.registry)
    if args.expected and len(rows) != args.expected:
        raise SystemExit(f"expected {args.expected} rows, found {len(rows)}")

    args.out.mkdir(parents=True)
    cases = args.out / "cases"
    cases.mkdir()
    manifest = []
    missing = []
    for row in rows:
        source = (args.data_root / row["image"]).resolve()
        if not source.is_file():
            missing.append({"id": row["id"], "image": str(source)})
            continue
        case = cases / row["id"]
        case.mkdir()
        suffix = source.suffix.lower() or ".png"
        target = case / f"image{suffix}"
        stage_file(source, target, args.mode)
        manifest.append(
            {
                "id": row["id"],
                "source": row.get("source"),
                "difficulty": row.get("difficulty"),
                "presentation": row.get("presentation") or [],
                "tier": row.get("tier"),
                "has_markush": bool(row.get("has_markush")),
                "image": target.relative_to(args.out).as_posix(),
                "image_sha256": sha256(target),
                "dataset_revision": args.dataset_revision,
                "reference_access": "forbidden_pre_seal",
            }
        )

    if missing:
        shutil.rmtree(args.out)
        sample = json.dumps(missing[:5], indent=2)
        raise SystemExit(f"missing {len(missing)} images; first entries:\n{sample}")

    manifest_path = args.out / "manifest.pre_reference.jsonl"
    with manifest_path.open("w", encoding="utf-8") as handle:
        for row in manifest:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    (args.out / "CORPUS_METADATA.json").write_text(
        json.dumps(
            {
                "schema": "chemrxn-blind-figure-corpus-v1",
                "count": len(manifest),
                "dataset_revision": args.dataset_revision,
                "registry_sha256": sha256(args.registry),
                "manifest_sha256": sha256(manifest_path),
                "contains_reference": False,
                "allowed_case_files_before_cache": ["image.png|jpg|jpeg|webp|tif|tiff"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(json.dumps({"out": str(args.out), "count": len(manifest), "gt_copied": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
