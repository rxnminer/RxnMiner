import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docs/results/pi-agent-pdfsi-extraction/build_review_summary.py"
EXTERNAL_FILES = (
    "perception_manifest.json",
    "perception_freeze.json",
    "extraction_summary.json",
    "coverage_report.json",
    "chemistry_validation.json",
    "alias_registry.json",
    "alias_resolution_audit.json",
    "full_reaction_closure_audit.json",
    "markush_templates.json",
    "EXTRACTION_REPORT.md",
    "UNEXTRACTABLE_REACTIONS.md",
    "FULL_REACTION_CLOSURE_AUDIT.md",
    "PI_AGENT_PDF_SI_DATAFLOW.html",
    "build_alias_registry.py",
    "build_predictions.py",
    "build_product_jobs.py",
    "validate_final_result.py",
    "refresh_final_reports.py",
    "audit_alias_resolution.py",
    "audit_full_reaction_closure.py",
    "pd/work/build_source_ledger.py",
    "bcu/work/build_source_ledger.py",
    "pd/pred_main.json",
    "pd/pred_si.json",
    "pd/work/chemistry_ledger.json",
    "pd/work/fhr_ledger.json",
    "pd/work/anchor_census.json",
    "pd/work/unresolved.json",
    "bcu/pred_main.json",
    "bcu/pred_si.json",
    "bcu/work/chemistry_ledger.json",
    "bcu/work/fhr_ledger.json",
    "bcu/work/anchor_census.json",
    "bcu/work/unresolved.json",
)


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)


def _fixture(tmp_path: Path, audit_status="PASS"):
    data = tmp_path / "acceptance"
    jobs = tmp_path / "jobs"
    out = tmp_path / "out"
    for relative in EXTERNAL_FILES:
        _write(data / relative, {})
    manifest = {"papers": {}}
    chemistry = {"status": "PASS", "predictions": {}}
    for paper in ("pd", "bcu"):
        manifest["papers"][paper] = {}
        chemistry["predictions"][paper] = {}
        for document in ("main", "si"):
            manifest["papers"][paper][document] = {
                "source_path": f"/private/source/{paper}-{document}.pdf",
                "source_sha256": "a" * 64,
                "n_pages": 1,
            }
            pred_path = data / paper / f"pred_{document}.json"
            _write(pred_path, {"anchors": []})
            chemistry["predictions"][paper][document] = {
                "status": "VALID",
                "summary": "VALID -- fixture",
                "sha256": hashlib.sha256(pred_path.read_bytes()).hexdigest(),
            }
        job_id = f"pdf_user-pdfsi-unseen-001_{paper}"
        _write(
            jobs / job_id / "work/reviewer_v2.json",
            {"review_summary": {"visible_records": 0}},
        )
        _write(jobs / job_id / "result.json", {})
        _write(jobs / job_id / "meta.json", {"filename": "Pi Agent提取的结果｜fixture"})
        _write(jobs / job_id / "work/pdf_view.json", {})
    _write(data / "perception_manifest.json", manifest)
    _write(data / "chemistry_validation.json", chemistry)
    _write(
        data / "full_reaction_closure_audit.json",
        {"status": audit_status, "totals": {"failures": 0}, "papers": {}},
    )
    _write(data / "alias_resolution_audit.json", {"status": "PASS", "papers": {}})
    _write(data / "markush_templates.json", {"templates": {"pd": [], "bcu": []}})
    return data, jobs, out


def test_review_summary_is_git_safe_and_checksummed(tmp_path):
    data, jobs, out = _fixture(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--data-root",
            str(data),
            "--jobs-root",
            str(jobs),
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    summary_text = (out / "review_summary.json").read_text()
    summary = json.loads(summary_text)
    assert summary["status"] == "final_source_corrected_with_explicit_exceptions"
    assert "/private/source" not in summary_text
    assert summary["documents"][0]["filename"].endswith(".pdf")
    checksum_lines = (out / "external_artifacts.sha256").read_text().splitlines()
    assert len(checksum_lines) == len(EXTERNAL_FILES) + 6
    assert all("acceptance/user-pdfsi-unseen-001/" in line or "  jobs/" in line for line in checksum_lines)
    assert not (out / "pd/pred_main.json").exists()
    assert f'"files": {len(checksum_lines)}' in result.stdout


def test_review_summary_refuses_failed_closure_audit(tmp_path):
    data, jobs, out = _fixture(tmp_path, audit_status="FAIL")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--data-root",
            str(data),
            "--jobs-root",
            str(jobs),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "closure/alias/chemistry validation is not PASS" in result.stderr
    assert not (out / "review_summary.json").exists()
