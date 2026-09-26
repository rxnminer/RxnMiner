"""UniParser snip recognition with the detection contract used by RxnMiner.

The SDK owns authentication and transport. This adapter preserves chemical wire
strings and converts source boxes to image pixels without canonicalizing chemistry.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any


class RecognitionError(RuntimeError):
    """Recognition failed before a usable, source-grounded result was available."""


def normalize_detections(result: dict[str, Any], image_size: tuple[int, int], image_md5: str) -> list[dict]:
    pages = result.get("pages_dict")
    if not isinstance(pages, list):
        raise RecognitionError("UniParser returned no pages_dict result.")
    width, height = image_size
    detections = []
    for page in pages:
        if not isinstance(page, list):
            raise RecognitionError("UniParser returned an invalid page layout.")
        for item in page:
            if not isinstance(item, dict) or item.get("type") != "molecule":
                continue
            box = item.get("bbox")
            if isinstance(box, dict):
                box = [box.get(key) for key in ("x1", "y1", "x2", "y2")]
            try:
                if not isinstance(box, (tuple, list)) or len(box) != 4:
                    raise ValueError
                x0, y0, x1, y1 = [float(value) for value in box]
                if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
                    raise ValueError
                if min(x0, y0) < 0 or x1 <= x0 or y1 <= y0:
                    raise ValueError
                # UniParser layout boxes are normalized. Some exported SDK
                # layouts carry pixels and their page_size; support both forms.
                if max(x0, y0, x1, y1) <= 1:
                    x0, x1, y0, y1 = x0 * width, x1 * width, y0 * height, y1 * height
                else:
                    page_width, page_height = item.get("page_size") or image_size
                    if page_width <= 0 or page_height <= 0 or x1 > page_width or y1 > page_height:
                        raise ValueError
                    x0, x1 = x0 * width / page_width, x1 * width / page_width
                    y0, y1 = y0 * height / page_height, y1 * height / page_height
            except (TypeError, ValueError, ZeroDivisionError):
                raise RecognitionError("UniParser returned an invalid molecule bounding box.") from None
            detection = {
                "bbox_index": len(detections),
                "bbox": [x0, y0, x1, y1],
                "score": item.get("conf", item.get("confidence")),
                "markush": bool(item.get("markush", False)),
                "md5": image_md5,
                "recognition_provider": "uniparser",
            }
            for field in ("esmi", "smi", "caption"):
                if isinstance(item.get(field), str):
                    detection[field] = item[field]
            # Legacy tools read caption first. SDK captions may be human names,
            # so use a chemical field explicitly and retain the caption separately.
            detection["source_caption"] = detection.get("caption", "")
            detection["caption"] = detection.get("esmi") or detection.get("smi") or ""
            detections.append(detection)
    return detections


def _require_success(response: Any, operation: str) -> dict:
    if not isinstance(response, dict) or response.get("status") != "success":
        # Do not echo service bodies, tokens, headers, or credentials to the UI.
        status = response.get("http_status") if isinstance(response, dict) else None
        hint = " Check UNIPARSER_API_KEY and service access." if status in (401, 403) else " Try again or check the service status."
        raise RecognitionError(f"UniParser {operation} failed.{hint}")
    return response


def recognize_molecules(image_path: str, timeout: int = 180, *, client=None) -> list[dict]:
    from PIL import Image

    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image not found: {path.name}")
    with Image.open(path) as image:
        size = image.size
    owned_client = client is None
    if owned_client:
        key = os.environ.get("UNIPARSER_API_KEY", "").strip()
        if not key:
            raise RecognitionError("Set UNIPARSER_API_KEY before recognizing molecules. Obtain a key at https://uniparser.dp.tech/.")
        try:
            from uniparser_tools.api.clients import UniParserClient
        except ImportError:
            raise RecognitionError("Install uniparser-tools in the RxnMiner Python environment.") from None
        client = UniParserClient(
            host=os.environ.get("UNIPARSER_BASE_URL") or os.environ.get("UNIPARSER_HOST") or "https://uniparser.dp.tech/",
            api_key=key,
        )
    try:
        # ParseMode.OCRFast=1; textual=False and padding_snip=False keep the
        # image coordinate frame unchanged. No whole-document parser is invoked.
        submitted = _require_success(client.trigger_snip(
            snip_path=str(path), sync=True, molecule=1, textual=False,
            padding_snip=False, timeout=timeout, http_timeout=(15, timeout + 30),
        ), "submission")
        token = submitted.get("token")
        if not isinstance(token, str) or not token:
            raise RecognitionError("UniParser returned no result token.")
        result = _require_success(client.get_result(
            token, pages_dict=True, molecule_source=True, http_timeout=(15, timeout),
        ), "result retrieval")
        return normalize_detections(result, size, hashlib.md5(path.read_bytes()).hexdigest())
    finally:
        if owned_client:
            client.close()
