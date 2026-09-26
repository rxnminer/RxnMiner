import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/skills/review-report/scripts/render_review_report.py"
SPEC = importlib.util.spec_from_file_location("review_report", SCRIPT)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MOD)


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d4944415408d763f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
        )
    )


def blind_fixture(tmp_path: Path):
    staging = tmp_path / "pass-a"
    case = staging / "cases/page-1"
    reviews = tmp_path / "outputs/pass-a/page-1"
    source = case / "source.png"
    evidence = reviews / "evidence/crop.png"
    png(source)
    png(evidence)
    task = {
        "schema": "chemrxn.blind-review-task.v1",
        "page_id": "page-1",
        "reviewer": "alice",
        "pass": "A",
        "lane": "human_settled",
        "input_manifest_sha256": "a" * 64,
        "source_sha256": sha(source),
        "allowed_files": ["candidate.v2.json", "mask_scope.json", "source.png"],
        "forbidden_lanes": ["pi_sealed"],
    }
    dump(case / "TASK.json", task)
    dump(staging / "STAGING.json", {"reviewer": "alice", "pass": "A", "cases": [{"page_id": "page-1", "case_dir": str(case)}]})
    review = {
        "schema": "chemrxn.source-review.v1",
        "page_id": "page-1",
        "reviewer": "alice",
        "pass": "A",
        "lane": "human_settled",
        "input_manifest_sha256": "a" * 64,
        "status": "complete",
        "page_disposition": "candidate_has_errors",
        "summary_zh": "来源图支持替换当前候选。",
        "findings": [{
            "finding_id": "F0001",
            "target_type": "molecule",
            "target_key": "selected_examples.0.products.0",
            "candidate_value": "CC",
            "source_visibility": "direct",
            "connectivity": "incorrect",
            "stereo": "not_applicable",
            "membership": "correct",
            "condition": "not_applicable",
            "metric": "not_applicable",
            "action": "replace",
            "correction": "CCC",
            "confidence": "high",
            "error_categories": ["connectivity"],
            "reason_zh": "来源图有三个碳。",
            "evidence": [{"kind": "source_crop", "path": "evidence/crop.png", "sha256": sha(evidence), "description_zh": "来源局部"}],
        }],
    }
    dump(reviews / "review.json", review)
    evidence_index = [{"path": "evidence/crop.png", "sha256": sha(evidence), "bytes": evidence.stat().st_size}]
    dump(reviews / "evidence-index.json", evidence_index)
    seal = {
        "schema": "chemrxn.source-review-seal.v1",
        "page_id": "page-1",
        "reviewer": "alice",
        "pass": "A",
        "lane": "human_settled",
        "input_manifest_sha256": "a" * 64,
        "review_sha256": sha(reviews / "review.json"),
        "evidence_index_sha256": sha(reviews / "evidence-index.json"),
    }
    dump(reviews / "SEAL.review.json", seal)
    return staging, reviews, seal


def test_blind_renderer_generates_single_lane_report(tmp_path):
    staging, reviews, _ = blind_fixture(tmp_path)
    out = tmp_path / "report"
    result = MOD.blind_report(staging, reviews.parent, out)
    page = (out / "index.html").read_text()
    assert result["pages_rendered"] == 1
    assert "来源图支持替换当前候选" in page
    assert "selected_examples.0.products.0" in page
    assert "pi_sealed" not in page
    assert (out / "assets").is_dir()


def test_blind_renderer_rejects_tampered_review(tmp_path):
    staging, reviews, _ = blind_fixture(tmp_path)
    data = json.loads((reviews / "review.json").read_text())
    data["summary_zh"] = "tampered"
    dump(reviews / "review.json", data)
    result = MOD.blind_report(staging, reviews.parent, tmp_path / "report")
    assert result["pages_rendered"] == 0
    assert "SHA mismatch" in result["failures"][0]["error"]


def test_coordinator_mode_fails_closed_until_unlocked_and_sealed(tmp_path):
    _, reviews, seal = blind_fixture(tmp_path)
    source = tmp_path / "coordinator-source.png"
    png(source)
    manifest = tmp_path / "coordinator.json"
    payload = {
        "schema": "chemrxn.coordinator-report-manifest.v1",
        "unlocked": False,
        "required_seals": [str(reviews / "SEAL.review.json")],
        "pages": [{"page_id": "page-1", "source": str(source), "disposition": "needs_fix", "summary_zh": "需修复", "findings": []}],
    }
    dump(manifest, payload)
    with pytest.raises(ValueError, match="not explicitly unlocked"):
        MOD.coordinator_report(manifest, tmp_path / "locked")
    payload["unlocked"] = True
    dump(manifest, payload)
    result = MOD.coordinator_report(manifest, tmp_path / "final")
    assert result["pages_rendered"] == 1
    assert "需修复" in (tmp_path / "final/index.html").read_text()
    assert result["required_seal_hashes"] == [sha(reviews / "SEAL.review.json")]
