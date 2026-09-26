"""Synfacts literature page images (PNG symlinks under ``image/``)."""
from __future__ import annotations

from pathlib import Path

from ma_paths import IMAGE_DIR


def safe_synfacts_image_filename(name: str) -> str:
    """Reject path traversal; require ``Synfacts-*-with-examples.png``."""
    base = Path(name).name
    if base != name or ".." in base:
        raise ValueError("invalid image filename")
    if not base.endswith("-with-examples.png"):
        raise ValueError("filename must end with -with-examples.png")
    if not base.startswith("Synfacts-"):
        raise ValueError("filename must start with Synfacts-")
    return base


def image_name_for_json(json_filename: str) -> str:
    """``Synfacts-…-with-examples.json`` → matching PNG basename."""
    base = Path(json_filename).name
    if base.endswith("-with-examples.json"):
        return base[: -len(".json")] + ".png"
    if base.endswith(".json"):
        return base[:-5] + ".png"
    return base + ".png"


def resolve_synfacts_page_image(json_filename: str) -> Path | None:
    """Return PNG path if a page image exists for this JSON file."""
    try:
        png_name = safe_synfacts_image_filename(image_name_for_json(json_filename))
    except ValueError:
        return None
    path = IMAGE_DIR / png_name
    if path.is_file():
        return path
    return None
