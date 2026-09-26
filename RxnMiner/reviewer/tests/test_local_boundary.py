from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_reviewer_rejects_cross_site_reads_and_dns_rebinding(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_ROOT", str(tmp_path))
    from reviewer.chem_reviewer import server

    with TestClient(server.app, base_url="http://127.0.0.1:8767") as client:
        cross_site = client.get(
            "/api/papers",
            headers={"Origin": "https://attacker.invalid"},
        )
        rebinding = client.get(
            "/api/papers",
            headers={"Host": "attacker.invalid"},
        )
        fetch_metadata = client.get(
            "/api/papers",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        same_origin = client.get(
            "/api/papers",
            headers={"Origin": "http://127.0.0.1:8767"},
        )

    assert cross_site.status_code == 403
    assert rebinding.status_code == 403
    assert fetch_metadata.status_code == 403
    assert same_origin.status_code == 200
    assert same_origin.headers["access-control-allow-origin"] == "http://127.0.0.1:8767"
    assert "frame-ancestors 'self'" in same_origin.headers["content-security-policy"]
    assert same_origin.headers["x-content-type-options"] == "nosniff"


def test_reviewer_accepts_the_explicit_local_harness_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_ROOT", str(tmp_path))
    from reviewer.chem_reviewer import server

    with TestClient(server.app, base_url="http://127.0.0.1:8767") as client:
        response = client.get(
            "/api/papers",
            headers={"Origin": "http://127.0.0.1:8785"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8785"


def test_figure_review_requires_preview_and_creates_immutable_versions(tmp_path, monkeypatch):
    from reviewer.chem_reviewer import server

    monkeypatch.setattr(server, "DATA_ROOT", tmp_path)
    paper = tmp_path / "upload_live"
    paper.mkdir()
    original = {
        "schema_version": "v2",
        "selected_examples": [{"id": "ex1", "metrics": {"yield": "45%"}}],
    }
    original_path = paper / "0_predicted_output.json"
    original_bytes = (server.json.dumps(original, indent=2) + "\n").encode()
    original_path.write_bytes(original_bytes)
    proposed = {
        **original,
        "selected_examples": [{"id": "ex1", "metrics": {"yield": "50%"}}],
    }

    with TestClient(server.app, base_url="http://127.0.0.1:8767") as client:
        preview = client.post(
            "/api/paper/upload_live/task/0/preview",
            json={"data": proposed},
        )
        assert preview.status_code == 200
        assert preview.json()["proposed"] is True
        assert preview.json()["next_version"] == 1
        assert preview.json()["base_version"] == 0
        assert preview.json()["source_sha256"]
        proposal_v1 = preview.json()["proposal_id"]
        assert preview.json()["n_ops"] > 0
        assert not list(paper.glob("*reviewed*"))

        bypass = client.post(
            "/api/paper/upload_live/task/0/save",
            json={"data": proposed},
        )
        assert bypass.status_code == 422

        saved_v1 = client.post(
            "/api/paper/upload_live/task/0/save",
            json={
                "data": proposed,
                "proposal_id": proposal_v1,
                "agent_proposal_id": "agent-proposal-1",
            },
        )
        assert saved_v1.status_code == 200
        assert saved_v1.json()["version"] == 1
        assert saved_v1.json()["parent_version"] is None
        assert saved_v1.json()["agent_proposal_id"] == "agent-proposal-1"
        snapshot_v1 = server.json.loads(
            paper.joinpath("0_predicted_output.reviewed-v1.json").read_text(),
        )
        assert snapshot_v1["_reviewed_meta"]["agent_proposal_id"] == "agent-proposal-1"

        proposed_v2 = {
            **original,
            "selected_examples": [{"id": "ex1", "metrics": {"yield": "55%"}}],
        }
        preview_v2 = client.post(
            "/api/paper/upload_live/task/0/preview",
            json={"data": proposed_v2},
        )
        assert preview_v2.status_code == 200
        assert preview_v2.json()["base_version"] == 1
        saved_v2 = client.post(
            "/api/paper/upload_live/task/0/save",
            json={
                "data": proposed_v2,
                "proposal_id": preview_v2.json()["proposal_id"],
            },
        )
        assert saved_v2.status_code == 200
        assert saved_v2.json()["version"] == 2
        assert saved_v2.json()["parent_version"] == 1

        versions = client.get("/api/paper/upload_live/task/0/versions")
        assert [item["version"] for item in versions.json()["versions"]] == [1, 2]
        assert [item["is_head"] for item in versions.json()["versions"]] == [False, True]
        version_one_diff = client.get(
            "/api/paper/upload_live/task/0/versions/1/diff",
        )
        assert version_one_diff.status_code == 200
        assert version_one_diff.json()["version"] == 1

        proposed_stale = {
            **original,
            "selected_examples": [{"id": "ex1", "metrics": {"yield": "60%"}}],
        }
        preview_stale = client.post(
            "/api/paper/upload_live/task/0/preview",
            json={"data": proposed_stale},
        ).json()
        proposed_v3 = {
            **original,
            "selected_examples": [{"id": "ex1", "metrics": {"yield": "65%"}}],
        }
        preview_v3 = client.post(
            "/api/paper/upload_live/task/0/preview",
            json={"data": proposed_v3},
        ).json()
        saved_v3 = client.post(
            "/api/paper/upload_live/task/0/save",
            json={
                "data": proposed_v3,
                "proposal_id": preview_v3["proposal_id"],
            },
        )
        assert saved_v3.status_code == 200
        stale = client.post(
            "/api/paper/upload_live/task/0/save",
            json={
                "data": proposed_stale,
                "proposal_id": preview_stale["proposal_id"],
            },
        )
        assert stale.status_code == 409

        undo_v3 = client.post("/api/paper/upload_live/task/0/undo", json={})
        assert undo_v3.status_code == 200
        assert undo_v3.json()["head_version"] == 2

        proposed_v4 = {
            **original,
            "selected_examples": [{"id": "ex1", "metrics": {"yield": "75%"}}],
        }
        preview_v4 = client.post(
            "/api/paper/upload_live/task/0/preview",
            json={"data": proposed_v4},
        ).json()
        saved_v4 = client.post(
            "/api/paper/upload_live/task/0/save",
            json={"data": proposed_v4, "proposal_id": preview_v4["proposal_id"]},
        )
        assert saved_v4.status_code == 200
        assert saved_v4.json()["version"] == 4
        assert saved_v4.json()["parent_version"] == 2
        undo_v4 = client.post("/api/paper/upload_live/task/0/undo", json={})
        assert undo_v4.json()["head_version"] == 2

        # Returning to the exact source/head makes the old proposal valid
        # again, but it creates a new immutable branch version.
        replay_v3 = client.post(
            "/api/paper/upload_live/task/0/save",
            json={
                "data": proposed_v3,
                "proposal_id": preview_v3["proposal_id"],
            },
        )
        assert replay_v3.status_code == 200
        assert replay_v3.json()["version"] == 5
        assert replay_v3.json()["parent_version"] == 2
        replay_again = client.post(
            "/api/paper/upload_live/task/0/save",
            json={
                "data": proposed_v3,
                "proposal_id": preview_v3["proposal_id"],
            },
        )
        assert replay_again.status_code == 409
        assert client.post(
            "/api/paper/upload_live/task/0/undo",
            json={},
        ).json()["head_version"] == 2

        undo_v2 = client.post("/api/paper/upload_live/task/0/undo", json={})
        assert undo_v2.status_code == 200
        assert undo_v2.json()["head_version"] == 1
        after_undo_v2 = client.get("/api/paper/upload_live/task/0")
        assert after_undo_v2.json()["reviewed_version"] == 1
        assert (
            after_undo_v2.json()["data"]["selected_examples"][0]["metrics"]["yield"]
            == "50%"
        )

        undo_v1 = client.post("/api/paper/upload_live/task/0/undo", json={})
        assert undo_v1.status_code == 200
        assert undo_v1.json()["head_version"] == 0
        assert client.get(
            "/api/paper/upload_live/task/0",
        ).json()["is_reviewed"] is False

        proposed_after_raw = {
            **original,
            "selected_examples": [{"id": "ex1", "metrics": {"yield": "70%"}}],
        }
        raw_bound_preview = client.post(
            "/api/paper/upload_live/task/0/preview",
            json={"data": proposed_after_raw},
        ).json()
        original_path.write_bytes(original_bytes + b" ")
        source_changed = client.post(
            "/api/paper/upload_live/task/0/save",
            json={
                "data": proposed_after_raw,
                "proposal_id": raw_bound_preview["proposal_id"],
            },
        )
        assert source_changed.status_code == 409
        assert not paper.joinpath("0_predicted_output.reviewed-v6.json").exists()
        original_path.write_bytes(original_bytes)

    assert original_path.read_bytes() == original_bytes
    assert paper.joinpath("0_predicted_output.reviewed-v1.json").exists()
    assert paper.joinpath("0_predicted_output.reviewed-v2.json").exists()
    assert paper.joinpath("0_predicted_output.reviewed-v3.json").exists()
    assert paper.joinpath("0_predicted_output.reviewed-v4.json").exists()
    assert paper.joinpath("0_predicted_output.reviewed-v5.json").exists()
    assert not paper.joinpath("0_predicted_output.reviewed.json").exists()


def test_figure_review_keeps_legacy_pointer_readable_and_undoable(tmp_path, monkeypatch):
    from reviewer.chem_reviewer import server

    monkeypatch.setattr(server, "DATA_ROOT", tmp_path)
    paper = tmp_path / "legacy"
    paper.mkdir()
    original = {
        "schema_version": "v2",
        "selected_examples": [{"id": "ex1", "metrics": {"yield": "45%"}}],
    }
    reviewed = {
        **original,
        "selected_examples": [{"id": "ex1", "metrics": {"yield": "50%"}}],
    }
    original_path = paper / "0_predicted_output.json"
    original_bytes = (server.json.dumps(original, indent=2) + "\n").encode()
    original_path.write_bytes(original_bytes)
    legacy_pointer = paper / "0_predicted_output.reviewed.json"
    legacy_pointer.write_bytes(
        (server.json.dumps(reviewed, indent=2).replace("\n", "\r\n") + "\r\n")
        .encode("utf-8")
    )

    with TestClient(server.app, base_url="http://127.0.0.1:8767") as client:
        task = client.get("/api/paper/legacy/task/0")
        assert task.status_code == 200
        assert task.json()["is_reviewed"] is True
        assert task.json()["reviewed_version"] is None
        preview = client.post(
            "/api/paper/legacy/task/0/preview",
            json={"data": {
                **original,
                "selected_examples": [{
                    "id": "ex1",
                    "metrics": {"yield": "55%"},
                }],
            }},
        )
        assert preview.status_code == 200
        assert preview.json()["base_version"] == 0
        assert preview.json()["base_sha256"]
        legacy_bytes = legacy_pointer.read_bytes()
        modern = client.post(
            "/api/paper/legacy/task/0/save",
            json={
                "data": {
                    **original,
                    "selected_examples": [{
                        "id": "ex1",
                        "metrics": {"yield": "55%"},
                    }],
                },
                "proposal_id": preview.json()["proposal_id"],
            },
        )
        assert modern.status_code == 200
        assert modern.json()["parent_legacy_sha256"] == preview.json()["base_sha256"]
        undo_modern = client.post("/api/paper/legacy/task/0/undo", json={})
        assert undo_modern.status_code == 200
        assert undo_modern.json()["head_kind"] == "legacy"
        assert legacy_pointer.read_bytes() == legacy_bytes
        undo = client.post("/api/paper/legacy/task/0/undo", json={})
        assert undo.status_code == 200
        assert undo.json()["undone_version"] == "legacy"

    assert original_path.read_bytes() == original_bytes
    assert not legacy_pointer.exists()


def test_figure_review_history_survives_a_corrupt_pointer(tmp_path, monkeypatch):
    from reviewer.chem_reviewer import server

    monkeypatch.setattr(server, "DATA_ROOT", tmp_path)
    paper = tmp_path / "corrupt"
    paper.mkdir()
    original = {"schema_version": "v2", "selected_examples": []}
    original_path = paper / "0_predicted_output.json"
    original_path.write_text(
        server.json.dumps(original),
        encoding="utf-8",
    )
    original_bytes = original_path.read_bytes()
    snapshot = {
        **original,
        "_reviewed_meta": {
            "version": 1,
            "parent_version": None,
            "source_sha256": server._sha256_bytes(original_bytes),
        },
    }
    (paper / "0_predicted_output.reviewed-v1.json").write_text(
        server.json.dumps(snapshot),
        encoding="utf-8",
    )
    (paper / "0_predicted_output.reviewed-v1.diff.json").write_text(
        '{"_meta":{"version":1},"ops":[]}',
        encoding="utf-8",
    )
    (paper / "0_predicted_output.reviewed-v1.diff.md").write_text(
        "recovery diff",
        encoding="utf-8",
    )
    (paper / "0_predicted_output.reviewed.json").write_text(
        "{broken",
        encoding="utf-8",
    )

    with TestClient(server.app, base_url="http://127.0.0.1:8767") as client:
        versions = client.get("/api/paper/corrupt/task/0/versions")
        assert versions.status_code == 200
        assert versions.json()["versions"][0]["is_head"] is None
        task = client.get("/api/paper/corrupt/task/0")
        assert task.status_code == 200
        assert task.json()["data"] == original
        assert task.json()["review_pointer_corrupt"] is True
        assert task.json()["reviewed_versions"][0]["version"] == 1
        manifest = client.get("/api/paper/corrupt/manifest")
        assert manifest.status_code == 200
        assert manifest.json()["tasks"]["0"]["is_reviewed"] is False
        assert manifest.json()["tasks"]["0"]["review_pointer_corrupt"] is True
        preview = client.post(
            "/api/paper/corrupt/task/0/preview",
            json={"data": original},
        )
        assert preview.status_code == 409
        damaged_diff = client.get("/api/paper/corrupt/task/0/diff")
        assert damaged_diff.status_code == 200
        assert damaged_diff.json()["review_pointer_corrupt"] is True
        assert damaged_diff.json()["history_available"] is True
        restored = client.post(
            "/api/paper/corrupt/task/0/versions/1/restore",
            json={},
        )
        assert restored.status_code == 200
        recovered = client.get("/api/paper/corrupt/task/0")
        assert recovered.json()["review_pointer_corrupt"] is False
        assert recovered.json()["reviewed_version"] == 1

        pointer = paper / "0_predicted_output.reviewed.json"
        pointer_before = pointer.read_bytes()
        original_path.write_bytes(original_bytes + b" ")
        raw_changed = client.post(
            "/api/paper/corrupt/task/0/versions/1/restore",
            json={},
        )
        assert raw_changed.status_code == 409
        assert pointer.read_bytes() == pointer_before
        original_path.write_bytes(original_bytes)

        incomplete = {
            **original,
            "_reviewed_meta": {
                "version": 2,
                "source_sha256": server._sha256_bytes(original_bytes),
            },
        }
        (paper / "0_predicted_output.reviewed-v2.json").write_text(
            server.json.dumps(incomplete),
            encoding="utf-8",
        )
        incomplete_restore = client.post(
            "/api/paper/corrupt/task/0/versions/2/restore",
            json={},
        )
        assert incomplete_restore.status_code == 409
        assert pointer.read_bytes() == pointer_before


def test_figure_review_inferred_parent_is_explicit_and_missing_parent_is_safe(
    tmp_path,
    monkeypatch,
):
    from reviewer.chem_reviewer import server

    monkeypatch.setattr(server, "DATA_ROOT", tmp_path)
    paper = tmp_path / "old_versions"
    paper.mkdir()
    original = {"schema_version": "v2", "selected_examples": []}
    (paper / "0_predicted_output.json").write_text(
        server.json.dumps(original),
        encoding="utf-8",
    )
    v1 = {
        **original,
        "_reviewed_meta": {"version": 1},
    }
    pointer = paper / "0_predicted_output.reviewed.json"
    pointer.write_text(server.json.dumps(v1), encoding="utf-8")
    (paper / "0_predicted_output.reviewed-v1.json").write_text(
        server.json.dumps(v1),
        encoding="utf-8",
    )
    (paper / "0_predicted_output.reviewed-v1.diff.json").write_text(
        '{"_meta":{"version":1},"ops":[]}',
        encoding="utf-8",
    )
    (paper / "0_predicted_output.reviewed-v1.diff.md").write_text(
        "old diff",
        encoding="utf-8",
    )

    with TestClient(server.app, base_url="http://127.0.0.1:8767") as client:
        inferred = client.post("/api/paper/old_versions/task/0/undo", json={})
        assert inferred.status_code == 200
        assert inferred.json()["head_kind"] == "raw"
        assert inferred.json()["parent_inferred"] is True

        v2 = {
            **original,
            "_reviewed_meta": {"version": 2, "parent_version": 9},
        }
        pointer.write_text(server.json.dumps(v2), encoding="utf-8")
        pointer_before = pointer.read_bytes()
        missing = client.post("/api/paper/old_versions/task/0/undo", json={})
        assert missing.status_code == 409
        assert pointer.read_bytes() == pointer_before
