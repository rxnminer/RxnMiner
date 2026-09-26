import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import harness_server as HS


def _make_shard(root: Path, *, sealed=False):
    shard = root / "shard"
    inputs = shard / "input"
    inputs.mkdir(parents=True)
    image = inputs / "case-a.png"
    Image.new("RGB", (32, 24), "white").save(image)
    cache = inputs / "case-a.molparser.json"
    cache.write_text('[{"bbox_index":0,"bbox":[1,1,10,10],"esmi":"CCO"}]')
    row = {
        "id": "case-a",
        "source": "pdf",
        "difficulty": "L2",
        "image": "input/case-a.png",
        "cache": "input/case-a.molparser.json",
        "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "cache_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
    }
    (shard / "manifest.pre_reference.jsonl").write_text(json.dumps(row) + "\n")
    (shard / "SHARD_METADATA.json").write_text(json.dumps({
        "shard_id": "shard-001", "repo_pin": "a" * 40, "contains_reference": False,
    }))
    (shard / "RUN_CODEX.md").write_text("blind")
    (shard / "run.sh").write_text("#!/bin/sh\n")
    if sealed:
        output_id = HS._distributed_case_output_id(row)
        output = root / "results" / "cases" / output_id / "output"
        output.mkdir(parents=True)
        prediction = {
            "schema_version": "2.0",
            "task_id": "case-a",
            "paper_id": "distributed",
            "anchor_image": "case-a.png",
            "reaction_templates": [],
            "selected_examples": [{
                "instance_id": "1",
                "reactants": [{"identifier": "1", "E-SMILES": "CCO"}],
                "products": [{"identifier": "2", "E-SMILES": "CC=O"}],
                "conditions": [], "metrics": [],
            }],
            "reaction_graph": [],
        }
        (output / "pred.v2.json").write_text(json.dumps(prediction))
        (output / "RUN_METADATA.json").write_text("{}")
        (output / "SEAL.pre_reference.json").write_text("{}")
    return shard, row


def test_distributed_archive_extraction_rejects_links_and_traversal(tmp_path):
    source = tmp_path / "source"
    shard, _ = _make_shard(source)
    archive = tmp_path / "valid.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(shard, arcname="shard-001")
    extracted = HS._distributed_safe_extract(archive, tmp_path / "extracted")
    assert extracted.name == "shard-001"
    assert (extracted / "input/case-a.png").is_file()

    unsafe = tmp_path / "unsafe.tar.gz"
    with tarfile.open(unsafe, "w:gz") as handle:
        info = tarfile.TarInfo("shard-001/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../gt.json"
        handle.addfile(info)
    with pytest.raises(ValueError, match="links/devices"):
        HS._distributed_safe_extract(unsafe, tmp_path / "unsafe-out")


def test_distributed_upload_endpoint_preflights_real_blind_package(tmp_path):
    old_runs, old_code, old_token = HS.DISTRIBUTED_RUNS_DIR, HS.DISTRIBUTED_CODE_ROOT, HS.PI_RUNTIME_INTERNAL_TOKEN
    try:
        HS.DISTRIBUTED_RUNS_DIR = tmp_path / "distributed"
        HS.DISTRIBUTED_CODE_ROOT = Path(__file__).resolve().parents[2]
        HS.PI_RUNTIME_INTERNAL_TOKEN = ""
        source = tmp_path / "source"
        shard, _ = _make_shard(source)
        archive = tmp_path / "shard-001.input.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(shard, arcname="shard-001")
        with TestClient(HS.app) as client, archive.open("rb") as stream:
            response = client.post(
                "/api/distributed/shards",
                files={"archive": (archive.name, stream, "application/gzip")},
            )
            assert response.status_code == 201, response.text
            body = response.json()
            assert body["total"] == 1
            assert body["status"] == "staged"
            assert body["preflight"]["valid"] is True
            listed = client.get("/api/distributed/shards").json()["batches"]
            assert [row["id"] for row in listed] == [body["id"]]
    finally:
        HS.DISTRIBUTED_RUNS_DIR, HS.DISTRIBUTED_CODE_ROOT = old_runs, old_code
        HS.PI_RUNTIME_INTERNAL_TOKEN = old_token


def test_distributed_start_uses_isolated_pi_profile_and_frozen_runner(tmp_path, monkeypatch):
    old = (
        HS.DISTRIBUTED_RUNS_DIR, HS.DISTRIBUTED_CODE_ROOT, HS.DISTRIBUTED_PI_AGENT_DIR,
        HS.PI_RUNTIME_INTERNAL_TOKEN, HS.LOCAL_RUNTIME_USER_ID,
    )
    try:
        HS.DISTRIBUTED_RUNS_DIR = tmp_path / "distributed"
        HS.DISTRIBUTED_CODE_ROOT = Path(__file__).resolve().parents[2]
        HS.DISTRIBUTED_PI_AGENT_DIR = tmp_path / "pi-auth"
        HS.DISTRIBUTED_PI_AGENT_DIR.mkdir()
        HS.PI_RUNTIME_INTERNAL_TOKEN = "internal"
        HS.LOCAL_RUNTIME_USER_ID = "local-user"
        pin = __import__("subprocess").check_output(
            ["git", "rev-parse", "HEAD"], cwd=HS.DISTRIBUTED_CODE_ROOT, text=True,
        ).strip()
        batch_id = "dist_aaaaaaaaaaaaaaaa"
        root = HS.DISTRIBUTED_RUNS_DIR / batch_id
        shard, _ = _make_shard(root)
        meta = {
            "id": batch_id, "owner": "local-user", "shardId": "shard-001", "status": "staged",
            "repoPin": pin, "created": 1, "updated": 1,
        }
        HS._distributed_write_meta(batch_id, meta)
        monkeypatch.setattr(HS, "_runtime_json", lambda *args, **kwargs: (200, b'{"configured":true}', "application/json"))
        monkeypatch.setattr(HS, "_distributed_process_alive", lambda pid: True)
        captured = {}

        class Process:
            pid = 43210

        def fake_popen(command, **kwargs):
            captured.update({"command": command, **kwargs})
            return Process()

        monkeypatch.setattr(HS.subprocess, "check_output", lambda *args, **kwargs: pin)
        monkeypatch.setattr(HS.subprocess, "Popen", fake_popen)
        with TestClient(HS.app) as client:
            response = client.post(f"/api/distributed/shards/{batch_id}/start", json={"concurrency": 2})
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "running"
        assert captured["env"]["PI_CODING_AGENT_DIR"] == str(HS.DISTRIBUTED_PI_AGENT_DIR)
        assert captured["env"]["CHEMRXN_REPO"] == str(HS.DISTRIBUTED_CODE_ROOT)
        assert "internal" not in json.dumps(captured["command"])
    finally:
        (
            HS.DISTRIBUTED_RUNS_DIR, HS.DISTRIBUTED_CODE_ROOT, HS.DISTRIBUTED_PI_AGENT_DIR,
            HS.PI_RUNTIME_INTERNAL_TOKEN, HS.LOCAL_RUNTIME_USER_ID,
        ) = old


def test_distributed_status_and_sealed_case_open_in_existing_figure_workspace(tmp_path):
    old_runs, old_jobs, old_pdf = HS.DISTRIBUTED_RUNS_DIR, HS.JOBS_DIR, HS.PDF_ROOT
    try:
        HS.DISTRIBUTED_RUNS_DIR = tmp_path / "distributed"
        HS.JOBS_DIR = tmp_path / "jobs"
        HS.PDF_ROOT = tmp_path / "reviewer"
        batch_id = "dist_0123456789abcdef"
        root = HS.DISTRIBUTED_RUNS_DIR / batch_id
        shard, _ = _make_shard(root, sealed=True)
        meta = {
            "id": batch_id, "owner": "alice", "shardId": "shard-001", "status": "running",
            "repoPin": "a" * 40, "created": 1, "updated": 1, "pid": -1,
        }
        HS._distributed_write_meta(batch_id, meta)
        status = HS._distributed_refresh(meta)
        assert status["sealed"] == 1
        assert status["cases"][0]["reviewable"] is True

        registered = HS._distributed_register_case(meta, "case-a", "alice")
        assert registered["workspaceUrl"].startswith("/?job=dist_")
        job_id = registered["jobId"]
        assert (HS.JOBS_DIR / job_id / "result.json").is_file()
        slug = f"upload_{job_id}"
        assert (HS.PDF_ROOT / slug / "0_predicted_output.json").is_file()
        assert (HS.PDF_ROOT / slug / "0_molparser.json").is_file()
        assert HS.JOBS[job_id]["owner"] == "alice"
    finally:
        HS.DISTRIBUTED_RUNS_DIR, HS.JOBS_DIR, HS.PDF_ROOT = old_runs, old_jobs, old_pdf
