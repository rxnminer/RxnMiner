#!/usr/bin/env python3
"""Create portable, checksummed participant input archives from validated blind shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

from validate_distributed_shard import validate_input


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"refusing to overwrite: {args.out}")
    shard_dirs = sorted(path for path in args.shards.glob("shard-*") if path.is_dir())
    if not shard_dirs:
        raise SystemExit("no shards found")
    args.out.mkdir(parents=True)
    index = []
    for shard in shard_dirs:
        rows, errors = validate_input(shard)
        if errors:
            raise SystemExit(f"{shard.name} failed validation: {errors[:3]}")
        archive = args.out / f"{shard.name}.input.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(shard, arcname=shard.name, recursive=True)
        digest = sha256(archive)
        checksum = archive.with_suffix(archive.suffix + ".sha256")
        checksum.write_text(f"{digest}  {archive.name}\n")
        index.append({"shard_id": shard.name, "count": len(rows), "archive": archive.name, "sha256": digest, "bytes": archive.stat().st_size})
        print(f"PACKAGED {shard.name} cases={len(rows)} bytes={archive.stat().st_size}", flush=True)
    (args.out / "PACKAGE_INDEX.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"packages": len(index), "cases": sum(item["count"] for item in index)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
