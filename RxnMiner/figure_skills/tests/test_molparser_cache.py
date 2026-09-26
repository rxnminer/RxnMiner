import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from tools import chem_mcp


DETECTIONS = [
    {
        "bbox_index": 0,
        "bbox": [1, 2, 30, 40],
        "caption": "EtOH<sep><a>0:Et</a>",
        "smi": "CCO",
        "esmi": "OCC<sep>",
        "score": 0.97,
        "markush": False,
        "raw_field": "preserved only in the cache",
    }
]


def image(tmp_path):
    path = tmp_path / "figure.png"
    path.write_bytes(b"new-figure-pixels")
    return path


def test_ocsr_detect_prefers_sibling_cache_without_network(tmp_path, monkeypatch):
    source = image(tmp_path)
    (tmp_path / "molparser.json").write_text(json.dumps(DETECTIONS))
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda unused: pytest.fail("live MolParser must not run on a cache hit"),
    )

    result = chem_mcp.tool_ocsr_detect(str(source))

    assert result["n"] == 1
    assert result["detections"][0]["esmi"] == "OCC<sep>"
    assert "smi" not in result["detections"][0]
    assert "caption" not in result["detections"][0]
    assert "raw_field" not in result["detections"][0]
    assert result["ocsr"]["source"] == "sibling_cache"
    assert result["ocsr"]["cache_hit"] is True
    assert result["ocsr"]["cache_path"] == str(tmp_path / "molparser.json")


def test_named_sibling_cache_wins_over_legacy_sibling(tmp_path, monkeypatch):
    source = image(tmp_path)
    named = tmp_path / "figure.molparser.json"
    named.write_text(json.dumps(DETECTIONS))
    (tmp_path / "molparser.json").write_text(json.dumps([{**DETECTIONS[0], "esmi": "N<sep>"}]))
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda unused: pytest.fail("live MolParser must not run on a cache hit"),
    )

    result = chem_mcp.tool_ocsr_detect(str(source))

    assert result["detections"][0]["esmi"] == "OCC<sep>"
    assert result["ocsr"]["source"] == "named_sibling_cache"
    assert result["ocsr"]["cache_path"] == str(named)


def test_global_md5_cache_is_compatible_with_existing_corpus(tmp_path, monkeypatch):
    source = image(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    digest = hashlib.md5(source.read_bytes()).hexdigest()
    cache_file = cache_dir / f"{digest}.json"
    cache_file.write_text(json.dumps(DETECTIONS))
    monkeypatch.setenv("OCSR_CACHE", str(cache_dir))
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda unused: pytest.fail("live MolParser must not run on a cache hit"),
    )

    result = chem_mcp.tool_ocsr_detect(str(source))

    assert result["ocsr"]["source"] == "md5_cache"
    assert result["ocsr"]["cache_key"] == digest
    assert result["ocsr"]["cache_path"] == str(cache_file)


def test_new_image_calls_live_once_and_persists_exact_raw_cache(tmp_path, monkeypatch):
    source = image(tmp_path)
    calls = []

    def fake_live(path):
        calls.append(Path(path))
        return DETECTIONS

    monkeypatch.setattr(chem_mcp, "_call_live_molparser", fake_live)
    first = chem_mcp.tool_ocsr_detect(str(source))
    second = chem_mcp.tool_ocsr_detect(str(source))

    named = tmp_path / "figure.molparser.json"
    conventional = tmp_path / "molparser.json"
    assert calls == [source]
    assert first["ocsr"]["source"] == "live_molparser"
    assert first["ocsr"]["cache_hit"] is False
    assert set(first["ocsr"]["persisted_paths"]) == {str(named), str(conventional)}
    assert json.loads(named.read_text()) == DETECTIONS
    assert json.loads(conventional.read_text()) == DETECTIONS
    assert second["ocsr"]["source"] == "named_sibling_cache"
    assert second["ocsr"]["cache_hit"] is True


def test_detect_returns_raw_artifact_and_visual_paths(tmp_path, monkeypatch):
    source = image(tmp_path)
    monkeypatch.setattr(chem_mcp, "_call_live_molparser", lambda unused: DETECTIONS)

    def annotate(unused_source, unused_detections, target):
        Path(target).write_bytes(b"overview")

    def grid(unused_detections, unused_source, target):
        Path(target).write_bytes(b"grid")

    monkeypatch.setitem(
        sys.modules,
        "ocsr_visualize",
        SimpleNamespace(annotate_detections=annotate, render_detections_grid=grid),
    )

    def audit_crops(unused_source, unused_detections, target_dir):
        path = Path(target_dir) / "audit-0.png"
        path.write_bytes(b"audit")
        return [{"bbox_index": 0, "path": str(path)}]

    monkeypatch.setattr(chem_mcp, "_write_concrete_audit_crops", audit_crops)
    output_dir = tmp_path / "output"
    result = chem_mcp.tool_detect(str(source), str(output_dir))

    assert result["ocsr"]["source"] == "live_molparser"
    assert json.loads(Path(result["raw_molparser_json"]).read_text()) == DETECTIONS
    assert Path(result["overview_png"]).read_bytes() == b"overview"
    assert Path(result["compare_grid_png"]).read_bytes() == b"grid"
    assert result["concrete_audit_crops"] == [{
        "bbox_index": 0, "path": str(output_dir / "audit-0.png"),
    }]
    assert Path(result["concrete_audit_crops"][0]["path"]).read_bytes() == b"audit"


def test_offline_cache_miss_fails_without_calling_live(tmp_path, monkeypatch):
    source = image(tmp_path)
    monkeypatch.setenv("OCSR_OFFLINE", "1")
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda unused: pytest.fail("offline mode must not call live MolParser"),
    )

    with pytest.raises(RuntimeError, match="MolParser cache miss"):
        chem_mcp.tool_ocsr_detect(str(source))


def test_hash_bound_cache_mismatch_falls_back_to_live(tmp_path, monkeypatch):
    source = image(tmp_path)
    (tmp_path / "figure.molparser.json").write_text(json.dumps({
        "image_sha256": "0" * 64,
        "detections": [{**DETECTIONS[0], "esmi": "wrong<sep>"}],
    }))
    calls = []
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda path: calls.append(Path(path)) or DETECTIONS,
    )

    result = chem_mcp.tool_ocsr_detect(str(source))

    assert calls == [source]
    assert result["ocsr"]["source"] == "live_molparser"
    assert result["detections"][0]["esmi"] == "OCC<sep>"


def test_force_live_refreshes_even_valid_cache(tmp_path, monkeypatch):
    source = image(tmp_path)
    (tmp_path / "molparser.json").write_text(json.dumps([{**DETECTIONS[0], "esmi": "old<sep>"}]))
    calls = []
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda path: calls.append(Path(path)) or DETECTIONS,
    )

    result = chem_mcp.tool_ocsr_detect(str(source), force_live=True)

    assert calls == [source]
    assert result["ocsr"]["source"] == "live_molparser"
    assert result["detections"][0]["esmi"] == "OCC<sep>"


def _full_parser_fixture(source, declared_md5=None):
    digest = hashlib.md5(source.read_bytes()).hexdigest()
    return {
        "status": "success",
        "md5": declared_md5 or digest,
        "content": "immutable owner parser response",
        "pages_dict": [[
            {
                "type": "molecule",
                "bbox": {"x1": 0.1, "y1": 0.25, "x2": 0.3, "y2": 0.5},
                "conf": 0.98,
                "caption": "EtOH<sep><a>0:Et</a>",
                "smi": "CCO",
                "esmi": "OCC<sep>",
                "markush": False,
            },
            {
                "type": "molecule",
                "bbox": {"x1": 0.4, "y1": 0.1, "x2": 0.6, "y2": 0.2},
                "conf": 0.1,
                # Preserve failed OCSR occurrences for image audit instead of dropping the bbox.
            },
            {"type": "paragraph", "bbox": {"x1": 0, "y1": 0, "x2": 1, "y2": 1}},
        ]],
    }


def _png_image(tmp_path):
    source = tmp_path / "figure.png"
    Image.new("RGB", (100, 80), "white").save(source)
    return source


def test_full_parser_result_cache_adapts_once_without_network(tmp_path, monkeypatch):
    source = _png_image(tmp_path)
    payload = _full_parser_fixture(source)
    (tmp_path / "molparser.json").write_text(json.dumps(payload))
    monkeypatch.setenv("OCSR_OFFLINE", "1")
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda unused: pytest.fail("full parser-result cache must prevent a live call"),
    )

    result = chem_mcp.tool_ocsr_detect(str(source))

    assert result["n"] == 2
    assert result["detections"][0] == {
        "bbox_index": 0,
        "bbox": [10, 20, 30, 40],
        "esmi": "OCC<sep>",
        "score": 0.98,
        "markush": False,
        "md5": payload["md5"],
    }
    assert result["detections"][1]["bbox"] == [40, 8, 60, 16]
    assert "esmi" not in result["detections"][1]
    assert "caption" not in result["detections"][0]
    assert "smi" not in result["detections"][0]
    assert result["ocsr"]["source"] == "sibling_cache"
    assert result["ocsr"]["cache_format"] == "full_parser_result"
    assert result["ocsr"]["image_md5"] == payload["md5"]


def test_full_parser_result_cache_rejects_wrong_image_md5(tmp_path, monkeypatch):
    source = _png_image(tmp_path)
    (tmp_path / "molparser.json").write_text(json.dumps(
        _full_parser_fixture(source, declared_md5="0" * 32),
    ))
    monkeypatch.setenv("OCSR_OFFLINE", "1")
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda unused: pytest.fail("offline mismatch must not call live MolParser"),
    )

    with pytest.raises(RuntimeError, match="MolParser cache miss"):
        chem_mcp.tool_ocsr_detect(str(source))


def test_detect_copies_full_parser_result_byte_for_byte(tmp_path, monkeypatch):
    source = _png_image(tmp_path)
    cache = tmp_path / "molparser.json"
    cache.write_text(json.dumps(_full_parser_fixture(source), indent=2) + "\n")
    original = cache.read_bytes()
    monkeypatch.setattr(
        chem_mcp,
        "_call_live_molparser",
        lambda unused: pytest.fail("full parser-result cache must prevent a live call"),
    )
    monkeypatch.setattr(
        chem_mcp,
        "_write_concrete_audit_crops",
        lambda unused_source, unused_detections, unused_dir: [],
    )

    def annotate(unused_source, unused_detections, target):
        Path(target).write_bytes(b"overview")

    def grid(unused_detections, unused_source, target):
        Path(target).write_bytes(b"grid")

    monkeypatch.setitem(
        sys.modules,
        "ocsr_visualize",
        SimpleNamespace(annotate_detections=annotate, render_detections_grid=grid),
    )
    output = tmp_path / "output"
    result = chem_mcp.tool_detect(str(source), str(output))

    assert result["ocsr"]["cache_format"] == "full_parser_result"
    assert Path(result["raw_molparser_json"]).read_bytes() == original
    assert cache.read_bytes() == original
