#!/usr/bin/env python3
"""Import one sealed, GT-free Figure prediction into Product and Chem Reviewer.

The prediction bytes are copied unchanged. This adapter adds only view metadata and a PNG rendering of
source image bytes when the input is not already PNG. It never reads GT, reviewed output, scores, or prior
predictions.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from PIL import Image

FORBIDDEN_KEYS = re.compile(r"(^|_)(gt|ground.?truth|accepted|reviewed|score|reference)(_|$)", re.I)
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe(value: str, limit: int = 48) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:limit] or "figure"


def reject_forbidden(value, where: str = "prediction") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if FORBIDDEN_KEYS.search(str(key)):
                raise ValueError(f"forbidden reference/evaluation key at {where}.{key}")
            reject_forbidden(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_forbidden(child, f"{where}[{index}]")


def load_prediction(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("prediction must be a JSON object")
    reject_forbidden(value)
    examples = value.get("selected_examples")
    templates = value.get("reaction_templates")
    if not isinstance(examples, list) or not isinstance(templates, list):
        raise ValueError("prediction must contain reaction_templates[] and selected_examples[]")
    if not examples:
        raise ValueError("prediction has no selected_examples")
    return value


def load_molparser(path: Path | None) -> list[dict]:
    if path is None:
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("detections")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("MolParser evidence must be a detection list or {detections:[...]}")
    return value


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_png(image: Path, destination: Path) -> None:
    if image.suffix.lower() == ".png":
        shutil.copyfile(image, destination)
        return
    with Image.open(image) as loaded:
        loaded.convert("RGB").save(destination, "PNG")


def upsert_collection(index: Path, card: dict) -> None:
    index.parent.mkdir(parents=True, exist_ok=True)
    lock = index.with_suffix(index.suffix + ".lock")
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        rows = []
        if index.exists():
            for line in index.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("id") != card["id"]:
                    rows.append(row)
        rows.append(card)
        payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        temporary = index.with_suffix(index.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, index)
        fcntl.flock(handle, fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--pred", type=Path, required=True)
    parser.add_argument("--molparser", type=Path)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--collection", default="external-figure")
    parser.add_argument("--title")
    args = parser.parse_args()

    image = args.image.expanduser().resolve(strict=True)
    pred = args.pred.expanduser().resolve(strict=True)
    molparser = args.molparser.expanduser().resolve(strict=True) if args.molparser else None
    if image.suffix.lower() not in SUPPORTED_IMAGES:
        raise ValueError(f"unsupported image extension: {image.suffix}")
    prediction = load_prediction(pred)
    detections = load_molparser(molparser)
    image_hash, pred_hash = sha256(image), sha256(pred)
    collection = safe(args.collection, 64)
    job_id = f"figure_{collection}_{image_hash[:12]}_{pred_hash[:12]}"
    reviewer_slug = f"upload_{job_id}"
    title = args.title or f"{image.stem} · {len(prediction['selected_examples'])} extracted reactions"
    data_root = args.data_root.expanduser().resolve()
    job = data_root / "jobs" / job_id
    reviewer = data_root / "reviewer" / reviewer_slug
    if job.exists() or reviewer.exists():
        existing = job / "result.json"
        if not existing.exists() or sha256(existing) != pred_hash:
            raise ValueError(f"refusing to overwrite existing imported result: {job_id}")
    job.mkdir(parents=True, exist_ok=True)
    (job / "work").mkdir(exist_ok=True)
    reviewer.mkdir(parents=True, exist_ok=True)

    # Preserve prediction bytes exactly; source_png is a view derivative, never the immutable source.
    shutil.copyfile(pred, job / "result.json")
    shutil.copyfile(pred, reviewer / "0_predicted_output.json")
    shutil.copyfile(image, job / f"input_source{image.suffix.lower()}")
    source_png(image, job / "input.png")
    shutil.copyfile(job / "input.png", job / "work/0.png")
    shutil.copyfile(job / "input.png", reviewer / "0.png")
    if molparser:
        shutil.copyfile(molparser, job / "work/0_molparser.json")
        shutil.copyfile(molparser, reviewer / "0_molparser.json")

    now = time.time()
    meta = {
        "id": job_id,
        "filename": title,
        "mode": "fast",
        "status": "done",
        "phase": "done",
        "n_examples": len(prediction["selected_examples"]),
        "owner": "local-user",
        "n_detections": len(detections),
        "created": now,
        "started": now,
        "ended": now,
        "valid": True,
        "reason": None,
        "reviewer_url": f"http://127.0.0.1:8767/static/#/{reviewer_slug}/0",
        "note": "GT-free external Figure prediction; persisted artifact replay",
        "provider": "external-agent",
        "model_name": None,
        "tag": collection,
        "source_sha256": image_hash,
        "prediction_sha256": pred_hash,
    }
    atomic_json(job / "meta.json", meta)
    atomic_json(reviewer / "reactions.json", {
        "paper_id": reviewer_slug,
        "title": title,
        "page_count": 1,
        "extractor": "GT-free external Figure prediction",
    })
    card = {key: meta.get(key) for key in (
        "id", "filename", "status", "phase", "n_examples", "n_detections", "created",
        "valid", "reviewer_url", "note", "provider", "model_name", "tag",
    )}
    upsert_collection(data_root / "jobs/_collections" / f"{collection}.jsonl", card)
    print(json.dumps({
        "job_id": job_id,
        "reviewer_slug": reviewer_slug,
        "reviewer_path": f"/static/#/{reviewer_slug}/0",
        "prediction_sha256": pred_hash,
        "source_sha256": image_hash,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
