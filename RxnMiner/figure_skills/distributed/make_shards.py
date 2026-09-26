#!/usr/bin/env python3
"""Create deterministic, source/difficulty-balanced blind runner shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections import Counter
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stage(source: Path, target: Path, copy: bool) -> None:
    if copy:
        shutil.copyfile(source, target)
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def stratum(row: dict) -> tuple:
    presentation = row.get("presentation") or []
    return (row.get("source") or "unknown", row.get("difficulty") or "unknown", presentation[0] if presentation else "unknown", bool(row.get("has_markush")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--repo-pin", required=True)
    parser.add_argument("--copy", action="store_true", help="copy instead of hardlinking files")
    args = parser.parse_args()
    if args.shards < 1:
        raise SystemExit("--shards must be positive")
    if args.out.exists():
        raise SystemExit(f"refusing to overwrite: {args.out}")

    rows = read_jsonl(args.corpus / "manifest.pre_reference.jsonl")
    if not rows:
        raise SystemExit("empty corpus manifest")
    assignments: list[list[dict]] = [[] for _ in range(args.shards)]
    strata_counts: list[Counter] = [Counter() for _ in range(args.shards)]
    for row in sorted(rows, key=lambda item: (stratum(item), hashlib.sha256(item["id"].encode()).hexdigest())):
        key = stratum(row)
        target = min(range(args.shards), key=lambda index: (strata_counts[index][key], len(assignments[index]), index))
        assignments[target].append(row)
        strata_counts[target][key] += 1

    args.out.mkdir(parents=True)
    index = []
    template = Path(__file__).with_name("RUN_CODEX.md")
    for number, assigned in enumerate(assignments, 1):
        shard_id = f"shard-{number:03d}"
        shard = args.out / shard_id
        inputs = shard / "input"
        inputs.mkdir(parents=True)
        output_rows = []
        for row in sorted(assigned, key=lambda item: item["id"]):
            source_image = args.corpus / row["image"]
            source_cache = source_image.with_name(f"{source_image.stem}.molparser.json")
            if not source_cache.is_file():
                raise SystemExit(f"missing cache for {row['id']}: {source_cache}")
            image_target = inputs / f"{row['id']}{source_image.suffix.lower()}"
            cache_target = inputs / f"{row['id']}.molparser.json"
            stage(source_image, image_target, args.copy)
            stage(source_cache, cache_target, args.copy)
            clean = dict(row)
            clean["image"] = image_target.relative_to(shard).as_posix()
            clean["cache"] = cache_target.relative_to(shard).as_posix()
            clean["cache_sha256"] = sha256(cache_target)
            output_rows.append(clean)
        manifest = shard / "manifest.pre_reference.jsonl"
        with manifest.open("w", encoding="utf-8") as handle:
            for row in output_rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        shutil.copyfile(template, shard / "RUN_CODEX.md")
        run_script = shard / "run.sh"
        run_script.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            ": \"${RXNMINER_REPO:?Set RXNMINER_REPO to the RxnMiner checkout}\"\n"
            ": \"${PI_CODING_AGENT_DIR:?Set PI_CODING_AGENT_DIR to your private Pi auth/config directory}\"\n"
            "exec \"$CHEMRXN_REPO/figure_skills/distributed/run_distributed_shard.sh\" \"$(cd \"$(dirname \"$0\")\" && pwd)\" \"$@\"\n"
        )
        run_script.chmod(0o755)
        metadata = {
            "schema": "chemrxn-distributed-shard-v1",
            "shard_id": shard_id,
            "count": len(output_rows),
            "manifest_sha256": sha256(manifest),
            "contains_reference": False,
            "repo_pin": args.repo_pin,
            "runtime": {
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "thinking": "medium",
                "skill": "reaction-extract",
                "perception": "offline_immutable_cache",
            },
            "sources": dict(sorted(Counter(row.get("source") for row in output_rows).items())),
            "difficulties": dict(sorted(Counter(row.get("difficulty") or "unknown" for row in output_rows).items())),
        }
        (shard / "SHARD_METADATA.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        index.append(metadata)

    (args.out / "SHARD_INDEX.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"out": str(args.out), "shards": args.shards, "cases": len(rows), "sizes": [len(items) for items in assignments]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
