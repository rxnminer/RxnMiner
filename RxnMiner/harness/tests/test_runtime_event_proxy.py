from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class RuntimeStub(BaseHTTPRequestHandler):
    requests: list[dict[str, str]] = []

    def do_GET(self):
        RuntimeStub.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "owner": self.headers.get("X-ChemRxn-User-Id", ""),
            "last_event_id": self.headers.get("Last-Event-ID", ""),
        })
        if self.path == "/healthz":
            body = json.dumps({
                "status": "degraded",
                "extractionBackend": "figure_pdf_pdf_si_live_ready",
                "interactivePiBackend": "local_api_key_or_pi_auth_ready",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/auth/status":
            body = json.dumps({
                "configured": True,
                "provider": "openai-codex",
                "profile": "chemrxn_isolated",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/sessions":
            body = json.dumps({
                "sessions": [{
                    "id": "session-a",
                    "jobId": "web-extract:test",
                    "task": "extract_all",
                    "status": "review_ready",
                    "authMode": "personal_codex_subscription",
                    "createdAt": "2000-01-01T00:00:00.000Z",
                    "updatedAt": "2000-01-01T00:01:00.000Z",
                }],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/sessions/session-a":
            body = json.dumps({
                "id": "session-a",
                "jobId": "web-extract:test",
                "task": "extract_all",
                "status": "review_ready",
                "authMode": "personal_codex_subscription",
                "createdAt": "2000-01-01T00:00:00.000Z",
                "updatedAt": "2000-01-01T00:01:00.000Z",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/sessions/session-a/dispatch":
            body = json.dumps({
                "workItem": {
                    "schemaVersion": "chemrxn.execution-work-item.v1",
                    "id": "wrk-session-a",
                    "sessionId": "session-a",
                    "state": "blocked",
                    "revision": 2,
                },
                "position": None,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "/terminal/" in self.path:
            self.send_response(204)
            self.end_headers()
            return
        if "/events?" in self.path:
            session_id = self.path.split("/sessions/", 1)[1].split("/", 1)[0]
            body = json.dumps({
                "events": [{
                    "seq": 5,
                    "sessionId": session_id,
                    "at": "2000-01-01T00:00:00.000Z",
                    "type": "agent.event",
                    "data": {"phase": "workspace.command"},
                }],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith("/result"):
            body = json.dumps({
                "schemaVersion": "chemrxn.runtime-result.v1",
                "gtIncluded": False,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/input-artifacts/input-a":
            body = b"immutable-source"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/sessions/session-a/live-artifacts/overview":
            body = b"live-overview"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/sessions/session-a/live-artifacts/pdf-main-page-1":
            body = b"live-pdf-page"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == f"/v1/sessions/session-a/live-artifacts/pdf-checkpoint-{'c' * 64}":
            body = b"immutable-pdf-checkpoint"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        session_id = self.path.split("/sessions/", 1)[1].split("/", 1)[0]
        event = {
            "seq": 5,
            "sessionId": session_id,
            "at": "2000-01-01T00:00:00.000Z",
            "type": "agent.event",
            "data": {"phase": "perception.started"},
        }
        body = f"id: 5\ndata: {json.dumps(event)}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(size)
        body = raw_body.decode(errors="replace")
        RuntimeStub.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "owner": self.headers.get("X-ChemRxn-User-Id", ""),
            "last_event_id": "",
            "body": body,
            "input_kind": self.headers.get("X-ChemRxn-Input-Kind", ""),
            "file_name": self.headers.get("X-ChemRxn-File-Name", ""),
        })
        if self.path == "/v1/input-artifacts":
            response = json.dumps({
                "artifact": {
                    "id": "input-artifact-a",
                    "kind": self.headers.get("X-ChemRxn-Input-Kind"),
                    "originalName": self.headers.get("X-ChemRxn-File-Name"),
                    "mediaType": self.headers.get("Content-Type"),
                    "sha256": "a" * 64,
                    "byteSize": len(raw_body),
                },
            }).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        if self.path == "/v1/models/resolve":
            request_body = json.loads(body)
            if request_body["modelId"] == "missing-model":
                response = json.dumps({
                    "error": {
                        "code": "MODEL_NOT_AVAILABLE",
                        "message": "Model openai/missing-model is not available in the locked Pi SDK",
                    },
                }).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return
            response = json.dumps({
                "available": True,
                "provider": request_body["provider"],
                "modelId": request_body["modelId"],
                "supportsImages": True,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        if self.path == "/v1/local-agent-sessions":
            response = json.dumps({
                "session": {
                    "id": "live-session-a",
                    "status": "ready",
                    "jobId": "web-extract:test",
                },
            }).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        if self.path.endswith("/retry"):
            response = json.dumps({
                "retried": True,
                "relation": "retry",
                "previousSessionId": "session-a",
                "session": {
                    "id": "retry-session-a",
                    "status": "running",
                    "jobId": "web-extract:test",
                },
            }).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        if self.path.endswith("/credentials"):
            response = json.dumps({
                "workItem": {
                    "schemaVersion": "chemrxn.execution-work-item.v1",
                    "id": "wrk-session-a",
                    "sessionId": "session-a",
                    "state": "pending",
                    "revision": 3,
                },
                "position": 1,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        if self.path.endswith("/review-outcomes"):
            request_body = json.loads(body)
            response = json.dumps({
                "event": {
                    "seq": 6,
                    "sessionId": "session-a",
                    "at": "2026-07-27T00:00:00.000Z",
                    "type": "review.outcome",
                    "data": request_body,
                },
                "idempotent": False,
            }).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        response = json.dumps({
            "accepted": True,
            "sessionId": self.path.split("/sessions/", 1)[1].split("/", 1)[0],
        }).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format, *_args):
        return


class ReviewerStub(BaseHTTPRequestHandler):
    requests: list[dict[str, str]] = []

    def do_GET(self):
        ReviewerStub.requests.append({"method": "GET", "path": self.path, "body": ""})
        if self.path.endswith("/anchor/0"):
            body = b"png-bytes"
            media_type = "image/png"
        else:
            body = json.dumps({"data": {"selected_examples": []}}).encode()
            media_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        ReviewerStub.requests.append({
            "method": "POST",
            "path": self.path,
            "body": body.decode(),
        })
        response = b'{"saved":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format, *_args):
        return


def test_runtime_proxy_keeps_internal_auth_server_side_and_propagates_terminal_204(tmp_path):
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    os.environ["CHEMRXN_PI_RUNTIME_BASE"] = f"http://127.0.0.1:{runtime.server_port}"
    os.environ["CHEMRXN_PI_INTERNAL_TOKEN"] = token
    try:
        from harness.service import harness_server

        original_jobs_dir = harness_server.JOBS_DIR
        original_pdf_root = harness_server.PDF_ROOT
        harness_server.JOBS_DIR = tmp_path / "jobs"
        harness_server.PDF_ROOT = tmp_path / "reviewer"
        figure_job = harness_server.JOBS_DIR / "web-extract:test"
        figure_review = harness_server.PDF_ROOT / "upload_runtime_test"
        figure_job.mkdir(parents=True)
        figure_review.mkdir(parents=True)
        (figure_job / "meta.json").write_text(json.dumps({
            "id": "web-extract:test",
            "reviewer_url": "http://127.0.0.1:8767/#/upload_runtime_test/0",
        }))
        (figure_review / "0_predicted_output.reviewed-v2.json").write_text(json.dumps({
            "_reviewed_meta": {
                "version": 2,
                "proposal_id": "reviewed-v2:abcdef",
                "agent_proposal_id": "proposal-1",
                "slug": "upload_runtime_test",
                "task_id": "0",
            },
        }))
        harness_server.AUTH_DIR = tmp_path / "auth"
        harness_server.SESSIONS_FILE = harness_server.AUTH_DIR / "sessions.json"
        harness_server.SESSIONS.clear()
        with TestClient(harness_server.app) as client:
            streamed = client.get(
                "/api/runtime/sessions/session-a/events?after=2",
                headers={"Last-Event-ID": "4"},
            )
            assert streamed.status_code == 200
            assert streamed.headers["content-type"].startswith("text/event-stream")
            assert "id: 5" in streamed.text
            assert token not in streamed.text
            assert "httponly" in streamed.headers["set-cookie"].lower()

            terminal = client.get("/api/runtime/sessions/terminal/events")
            assert terminal.status_code == 204

            harness_server.LOCAL_RUNTIME_USER_ID = "local-user"
            event_log = client.get("/api/runtime/sessions/session-a/event-log?after=2")
            assert event_log.status_code == 200
            assert event_log.json()["events"][0]["seq"] == 5
            assert token not in event_log.text
            result = client.get("/api/runtime/sessions/session-a/result")
            assert result.status_code == 200
            assert result.json()["gtIncluded"] is False
            assert token not in result.text

            message = client.post(
                "/api/runtime/sessions/session-a/messages",
                json={"text": "Inspect compound 12 stereo", "behavior": "steer"},
            )
            assert message.status_code == 202
            assert message.json()["accepted"] is True
            abort = client.post("/api/runtime/sessions/session-a/abort")
            assert abort.status_code == 202
            retry = client.post("/api/runtime/sessions/session-a/retry")
            assert retry.status_code == 201
            assert retry.json()["session"]["id"] == "retry-session-a"
            outcome_body = {
                "proposalId": "proposal-1",
                "jobId": "web-extract:test",
                "target": "figure",
                "status": "applied",
                "reviewedVersion": 2,
                "patchId": "reviewed-v2:abcdef",
            }
            outcome = client.post(
                "/api/runtime/sessions/session-a/review-outcomes",
                json=outcome_body,
            )
            assert outcome.status_code == 201
            assert outcome.json()["event"]["type"] == "review.outcome"
            forged = client.post(
                "/api/runtime/sessions/session-a/review-outcomes",
                json={**outcome_body, "patchId": "reviewed-v2:forged"},
            )
            assert forged.status_code == 409
            assert "snapshot does not match" in forged.json()["detail"]
            misattributed = client.post(
                "/api/runtime/sessions/session-a/review-outcomes",
                json={**outcome_body, "proposalId": "proposal-unrelated"},
            )
            assert misattributed.status_code == 409
            assert "snapshot does not match" in misattributed.json()["detail"]
            escaped = client.post(
                "/api/runtime/sessions/session-a/review-outcomes",
                json={
                    "proposalId": "proposal-escape",
                    "jobId": "../outside",
                    "target": "figure",
                    "status": "staged",
                },
            )
            assert escaped.status_code == 400

        first, second, event_snapshot, third, fourth, fifth, sixth = RuntimeStub.requests[:7]
        preflights = [
            item for item in RuntimeStub.requests
            if item["path"] == "/v1/sessions/session-a"
        ]
        outcome_posts = [
            item for item in RuntimeStub.requests
            if item["path"].endswith("/review-outcomes")
        ]
        assert first["authorization"] == f"Bearer {token}"
        assert first["last_event_id"] == "4"
        assert first["path"].endswith("/stream?after=2")
        assert first["owner"].startswith("guest:")
        assert second["owner"] == first["owner"]
        assert event_snapshot["owner"] == "local-user"
        assert event_snapshot["path"].endswith("/events?after=2")
        assert third["owner"] == "local-user"
        assert third["path"].endswith("/result")
        assert fourth["owner"] == "local-user"
        assert fourth["path"].endswith("/messages")
        assert json.loads(fourth["body"]) == {
            "text": "Inspect compound 12 stereo",
            "behavior": "steer",
        }
        assert fifth["path"].endswith("/abort")
        assert sixth["path"].endswith("/retry")
        assert json.loads(sixth["body"]) == {}
        assert sixth["authorization"] == f"Bearer {token}"
        assert token not in fourth["body"]
        # The runtime binds the disposition to the persisted allowlisted
        # proposal.  The proxy must not substitute the lifecycle session job
        # ID, because a linked task can control its sealed parent's workspace.
        assert len(preflights) == 0
        assert len(outcome_posts) == 1
        seventh = outcome_posts[0]
        assert json.loads(seventh["body"]) == outcome_body
        assert seventh["authorization"] == f"Bearer {token}"
    finally:
        if "harness_server" in locals():
            harness_server.LOCAL_RUNTIME_USER_ID = ""
            harness_server.JOBS_DIR = original_jobs_dir
            harness_server.PDF_ROOT = original_pdf_root
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_pdf_document_catalog_respects_namespaced_prebuilt_page_geometry(tmp_path):
    """A viewer-ready handoff may carry both Main and SI pages in one view."""
    import fitz
    from harness.service import harness_server

    work = tmp_path / "job" / "work"
    work.mkdir(parents=True)
    main = fitz.open()
    main.new_page(width=300, height=200)
    main_bytes = main.tobytes()
    main.close()
    si = fitz.open()
    si.new_page(width=400, height=600)
    si.new_page(width=410, height=610)
    si_bytes = si.tobytes()
    si.close()
    work.joinpath("main.pdf").write_bytes(main_bytes)
    work.joinpath("si.pdf").write_bytes(si_bytes)
    work.joinpath("documents.json").write_text(json.dumps({
        "documents": [
            {
                "source": "main",
                "pageCount": 1,
                "coordinateFrame": "pdf_points",
            },
            {
                "source": "si",
                "pageCount": 2,
                "coordinateFrame": "pdf_points",
                "coordinateDpi": 72,
            },
        ],
    }))
    view = {
        "pages": [
            {
                "page": 1,
                "width": 300,
                "height": 200,
                "source_document": "main",
            },
            {
                "page": 1,
                "width": 400,
                "height": 600,
                "source_document": "si",
            },
            {
                "page": 2,
                "width": 410,
                "height": 610,
                "source_document": "si",
            },
        ],
    }

    catalog = harness_server._pdf_document_catalog(tmp_path / "job", view)

    assert [(item["source"], item["pageCount"]) for item in catalog] == [
        ("main", 1),
        ("si", 2),
    ]
    assert catalog[0]["pages"] == [view["pages"][0]]
    assert catalog[1]["pages"] == view["pages"][1:]
    assert catalog[1]["coordinateFrame"] == "pdf_points"
    assert catalog[1]["coordinateDpi"] == 72


def test_web_runtime_capabilities_are_truthful_and_secret_free():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = token
        with TestClient(harness_server.app) as client:
            response = client.get("/api/runtime/capabilities")
        assert response.status_code == 200
        assert response.json() == {
            "connected": True,
            "extractionBackend": "figure_pdf_pdf_si_live_ready",
            "interactivePiBackend": "local_api_key_or_pi_auth_ready",
            "inputs": {"figure": True, "paper": True, "paper-si": True},
            "message": "Figure, Paper, and Paper + SI extraction are connected.",
        }
        assert token not in response.text
        assert RuntimeStub.requests == [{
            "path": "/healthz",
            "authorization": "",
            "owner": "",
            "last_event_id": "",
        }]
    finally:
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_applied_pdf_outcome_requires_the_committed_immutable_snapshot(tmp_path):
    from fastapi import HTTPException
    from harness.service import harness_server

    original_jobs_dir = harness_server.JOBS_DIR
    try:
        harness_server.JOBS_DIR = tmp_path / "jobs"
        job_dir = harness_server.JOBS_DIR / "paper-job"
        (job_dir / "review").mkdir(parents=True)
        (job_dir / "meta.json").write_text(json.dumps({"mode": "pdf"}))
        (job_dir / "review" / "reviewed-v3.json").write_text(json.dumps({
            "version": 3,
            "patch_id": "reviewed-v3:abcdef",
            "agent_proposal_id": "proposal-pdf",
        }))
        harness_server._verify_applied_review_outcome({
            "proposalId": "proposal-pdf",
            "jobId": "paper-job",
            "target": "pdf",
            "status": "applied",
            "reviewedVersion": 3,
            "patchId": "reviewed-v3:abcdef",
        })
        try:
            harness_server._verify_applied_review_outcome({
                "proposalId": "proposal-pdf",
                "jobId": "paper-job",
                "target": "pdf",
                "status": "applied",
                "reviewedVersion": 3,
                "patchId": "reviewed-v3:forged",
            })
        except HTTPException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("forged PDF patch ID was accepted")
        try:
            harness_server._verify_applied_review_outcome({
                "proposalId": "proposal-unrelated",
                "jobId": "paper-job",
                "target": "pdf",
                "status": "applied",
                "reviewedVersion": 3,
                "patchId": "reviewed-v3:abcdef",
            })
        except HTTPException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("unrelated agent proposal was attributed to the PDF snapshot")
    finally:
        harness_server.JOBS_DIR = original_jobs_dir


def test_pdf_review_and_raw_artifacts_are_isolated_by_signed_owner(tmp_path):
    from harness.service import harness_server

    original_jobs_dir = harness_server.JOBS_DIR
    original_local_owner = harness_server.LOCAL_RUNTIME_USER_ID
    original_sessions = dict(harness_server.SESSIONS)
    try:
        harness_server.JOBS_DIR = tmp_path / "jobs"
        # A clone-first launcher also has a fixed runtime owner. Signed browser
        # owners must still take precedence for classic uploads.
        harness_server.LOCAL_RUNTIME_USER_ID = "local-user"
        harness_server.SESSIONS.clear()
        harness_server.SESSIONS.update({
            "alice-token": {"user": "alice", "created": 1.0},
            "bob-token": {"user": "bob", "created": 1.0},
        })
        job_id = "alice-paper"
        job_dir = harness_server.JOBS_DIR / job_id
        (job_dir / "work").mkdir(parents=True)
        (job_dir / "meta.json").write_text(json.dumps({
            "id": job_id,
            "mode": "pdf",
            "owner": "alice",
            "filename": "paper.pdf",
        }))
        raw_bytes = json.dumps({
            "schema_version": "rxnharbor-pred-v2",
            "paper_id": "owner-test",
            "anchors": [{
                "anchor_figure": "Scheme 1",
                "anchor_page": 1,
                "selected_examples": [{
                    "instance_id": "1",
                    "reactants": [],
                    "products": [],
                    "conditions": [],
                    "metrics": [],
                }],
            }],
        }, separators=(",", ":")).encode()
        (job_dir / "result.json").write_bytes(raw_bytes)
        (job_dir / "work" / "pdf_view.json").write_text(json.dumps({
            "schema": "pdf_points_v1",
            "pages": [],
            "anchors": [],
            "molecules": [],
        }))
        (job_dir / "work" / "reviewer_v2.json").write_text(json.dumps({
            "schema": "pdf_reviewer_v2_evidence_v1",
            "rows": [{"id": "pred:0:0"}],
        }))
        import fitz
        source_document = fitz.open()
        source_document.new_page(width=300, height=200)
        (job_dir / "work" / "main.pdf").write_bytes(source_document.tobytes())
        source_document.close()
        proposal_body = {
            "base_version": 0,
            "after": {
                "difference_id": "pred:0:0",
                "verdict": "mark uncertain",
                "note": "owner-only review",
                "correction": None,
                "updated_at": 1.0,
            },
            "proposed_at": "2026-07-27T00:00:00Z",
        }

        with TestClient(harness_server.app) as alice:
            alice.cookies.set(harness_server.COOKIE, "alice-token")
            own_export = alice.get(f"/api/jobs/{job_id}/export?fmt=v2")
            own_versions = alice.get(f"/api/jobs/{job_id}/pdf/review/versions")
            own_preview = alice.post(
                f"/api/jobs/{job_id}/pdf/review/preview",
                json=proposal_body,
            )
        assert own_export.status_code == 200
        assert own_export.content == raw_bytes
        assert own_versions.status_code == 200
        assert own_preview.status_code == 200

        with TestClient(harness_server.app) as bob:
            bob.cookies.set(harness_server.COOKIE, "bob-token")
            assert bob.get(f"/api/jobs/{job_id}").status_code == 403
            assert bob.get(f"/api/jobs/{job_id}/result").status_code == 403
            assert bob.get(f"/api/jobs/{job_id}/export?fmt=v2").status_code == 403
            assert bob.get(f"/api/jobs/{job_id}/pdf/meta").status_code == 403
            assert bob.get(f"/api/jobs/{job_id}/pdf/review/versions").status_code == 403
            assert bob.get(f"/api/jobs/{job_id}/review/export").status_code == 403
            cross_apply = bob.post(
                f"/api/jobs/{job_id}/pdf/review/apply",
                json={"proposal": own_preview.json()},
            )
        assert cross_apply.status_code == 403
        assert not job_dir.joinpath("review").exists()

        local_job = harness_server.JOBS_DIR / "runtime-paper"
        local_job.mkdir()
        (local_job / "meta.json").write_text(json.dumps({
            "id": "runtime-paper",
            "mode": "pdf",
            "owner": "local-user",
        }))
        (local_job / "result.json").write_bytes(raw_bytes)
        with TestClient(harness_server.app) as signed_classic_browser:
            signed_classic_browser.cookies.set(harness_server.COOKIE, "alice-token")
            mixed_workflow_result = signed_classic_browser.get(
                "/api/jobs/runtime-paper/result",
            )
        assert mixed_workflow_result.status_code == 200
        with TestClient(harness_server.app) as cookie_free_local:
            local_result = cookie_free_local.get("/api/jobs/runtime-paper/result")
        assert local_result.status_code == 200

        with pytest.raises(HTTPException) as error:
            harness_server._verify_applied_review_outcome({
                "proposalId": "proposal-cross-owner",
                "jobId": job_id,
                "target": "pdf",
                "status": "applied",
                "reviewedVersion": 1,
                "patchId": "reviewed-v1:cross-owner",
            }, owner="bob")
        assert error.value.status_code == 403
    finally:
        harness_server.JOBS_DIR = original_jobs_dir
        harness_server.LOCAL_RUNTIME_USER_ID = original_local_owner
        harness_server.SESSIONS.clear()
        harness_server.SESSIONS.update(original_sessions)


def test_web_runtime_capabilities_report_disconnected_without_internal_token():
    from harness.service import harness_server

    previous = harness_server.PI_RUNTIME_INTERNAL_TOKEN
    harness_server.PI_RUNTIME_INTERNAL_TOKEN = ""
    try:
        with TestClient(harness_server.app) as client:
            response = client.get("/api/runtime/capabilities")
        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert not any(response.json()["inputs"].values())
        assert "not connected" in response.json()["message"].lower()
    finally:
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = previous


def test_bundled_product_sources_are_two_figures_without_gt_paths():
    from harness.service import harness_server

    with TestClient(harness_server.app) as client:
        response = client.get("/api/samples")
        assert response.status_code == 200
        samples = response.json()
        assert [(sample["id"], sample["sourceType"]) for sample in samples] == [
            ("example-job-scheme2", "figure"),
            ("example-job-scheme3", "figure"),
        ]
        assert all("gt" not in json.dumps(sample).lower() for sample in samples)
        for sample in samples:
            assert sample["rights"]["status"] == "verified"
            for source in sample["files"]:
                response = client.get(source["url"])
                assert response.status_code == 200
                assert response.content


def test_web_pi_auth_status_is_safe_and_uses_the_isolated_profile():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = token
        harness_server.LOCAL_RUNTIME_USER_ID = "local-auth-user"
        with TestClient(harness_server.app) as client:
            response = client.get("/api/runtime/auth/status")
        assert response.status_code == 200
        assert response.json() == {
            "configured": True,
            "provider": "openai-codex",
            "profile": "chemrxn_isolated",
            "loginCommand": "./chemrxn login",
        }
        assert "auth.json" not in response.text
        assert token not in response.text
        assert RuntimeStub.requests[0]["path"] == "/v1/auth/status"
        assert RuntimeStub.requests[0]["authorization"] == f"Bearer {token}"
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_web_runtime_history_is_tenant_scoped_and_uses_runtime_sessions():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = token
        harness_server.LOCAL_RUNTIME_USER_ID = "local-history-user"
        with TestClient(harness_server.app) as client:
            response = client.get("/api/runtime/sessions")
        assert response.status_code == 200
        assert response.json()["sessions"][0]["id"] == "session-a"
        assert RuntimeStub.requests[0]["path"] == "/v1/sessions"
        assert RuntimeStub.requests[0]["owner"] == "local-history-user"
        assert RuntimeStub.requests[0]["authorization"] == f"Bearer {token}"
        assert token not in response.text
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_runtime_dispatch_and_one_shot_queue_credentials_stay_tenant_scoped():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    key = "TEST_ONLY_one-run-memory-only"
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = token
        harness_server.LOCAL_RUNTIME_USER_ID = "local-queue-user"
        with TestClient(harness_server.app) as client:
            dispatch = client.get("/api/runtime/sessions/session-a/dispatch")
            resumed = client.post(
                "/api/runtime/sessions/session-a/credentials",
                json={"apiKey": key},
            )
            extra_field = client.post(
                "/api/runtime/sessions/session-a/credentials",
                json={"apiKey": key, "remember": True},
            )
            too_short = client.post(
                "/api/runtime/sessions/session-a/credentials",
                json={"apiKey": "short"},
            )

        assert dispatch.status_code == 200
        assert dispatch.json()["workItem"]["state"] == "blocked"
        assert dispatch.headers["cache-control"] == "private, no-store"
        assert resumed.status_code == 200
        assert resumed.json()["workItem"]["state"] == "pending"
        assert resumed.json()["position"] == 1
        assert key not in resumed.text
        assert extra_field.status_code == 400
        assert too_short.status_code == 400

        assert len(RuntimeStub.requests) == 2
        dispatch_request, credential_request = RuntimeStub.requests
        assert dispatch_request["path"] == "/v1/sessions/session-a/dispatch"
        assert dispatch_request["owner"] == "local-queue-user"
        assert dispatch_request["authorization"] == f"Bearer {token}"
        assert credential_request["path"] == "/v1/sessions/session-a/credentials"
        assert credential_request["owner"] == "local-queue-user"
        assert credential_request["authorization"] == f"Bearer {token}"
        assert json.loads(credential_request["body"]) == {"apiKey": key}
        assert key not in credential_request["path"]
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_local_product_boundary_rejects_cross_site_reads_writes_and_rebinding_host():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = token
        harness_server.LOCAL_RUNTIME_USER_ID = "local-csrf-user"
        with TestClient(harness_server.app, base_url="http://127.0.0.1") as client:
            cross_site = client.post(
                "/api/runtime/sessions/session-a/abort",
                headers={"Origin": "https://attacker.invalid"},
            )
            rebinding = client.post(
                "/api/runtime/sessions/session-a/abort",
                headers={"Host": "attacker.invalid"},
            )
            cross_site_read = client.get(
                "/api/runtime/sessions",
                headers={"Origin": "https://attacker.invalid"},
            )
            rebinding_read = client.get(
                "/api/runtime/sessions",
                headers={"Host": "attacker.invalid"},
            )
            fetch_metadata_read = client.get(
                "/api/runtime/sessions",
                headers={"Sec-Fetch-Site": "cross-site"},
            )
            cross_site_job_write = client.post(
                "/api/jobs/runtime_pdf_test/pdf/review/undo",
                headers={"Origin": "https://attacker.invalid"},
                json={"head_version": 1},
            )
            rebinding_job_index = client.get(
                "/api/jobs?all=1",
                headers={"Host": "attacker.invalid"},
            )
            fetch_metadata_job_read = client.get(
                "/api/jobs/runtime_pdf_test/export?fmt=v2",
                headers={
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                },
            )
            top_level_navigation = client.get(
                "/",
                headers={
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                },
            )
            same_origin = client.post(
                "/api/runtime/sessions/session-a/abort",
                headers={"Origin": "http://127.0.0.1"},
            )
            same_origin_read = client.get(
                "/api/runtime/sessions",
                headers={"Origin": "http://127.0.0.1"},
            )
        assert cross_site.status_code == 403
        assert rebinding.status_code == 403
        assert cross_site_read.status_code == 403
        assert rebinding_read.status_code == 403
        assert fetch_metadata_read.status_code == 403
        assert cross_site_job_write.status_code == 403
        assert rebinding_job_index.status_code == 403
        assert fetch_metadata_job_read.status_code == 403
        assert top_level_navigation.status_code == 200
        assert "ChemRxn" in top_level_navigation.text
        assert same_origin.status_code == 202
        assert same_origin_read.status_code == 200
        assert len(RuntimeStub.requests) == 2
        assert RuntimeStub.requests[0]["path"].endswith("/abort")
        assert RuntimeStub.requests[1]["path"] == "/v1/sessions"
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_runtime_message_proxy_forwards_one_time_recovery_key_without_echoing_it():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    key = "TEST_ONLY_reopen-only-in-memory"
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = "internal-test-token"
        harness_server.LOCAL_RUNTIME_USER_ID = "local-recovery-user"
        with TestClient(harness_server.app) as client:
            response = client.post(
                "/api/runtime/sessions/session-a/messages",
                json={
                    "text": "Resume this conversation",
                    "behavior": "prompt",
                    "apiKey": key,
                },
            )
        assert response.status_code == 202
        assert key not in response.text
        assert json.loads(RuntimeStub.requests[0]["body"]) == {
            "text": "Resume this conversation",
            "behavior": "prompt",
            "apiKey": key,
        }
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_same_origin_reviewer_bridge_reuses_task_and_image_apis_with_csrf_guard():
    ReviewerStub.requests = []
    reviewer = ThreadingHTTPServer(("127.0.0.1", 0), ReviewerStub)
    thread = threading.Thread(target=reviewer.serve_forever, daemon=True)
    thread.start()
    harness_server = None
    previous_local_owner = None
    previous_jobs = {}
    try:
        from harness.service import harness_server

        harness_server.REVIEWER_BASE = f"http://127.0.0.1:{reviewer.server_port}"
        previous_local_owner = harness_server.LOCAL_RUNTIME_USER_ID
        previous_jobs = {
            job_id: harness_server.JOBS.get(job_id)
            for job_id in ("a", "other")
        }
        harness_server.LOCAL_RUNTIME_USER_ID = "local-reviewer-owner"
        harness_server.JOBS["a"] = {"owner": "local-reviewer-owner"}
        harness_server.JOBS["other"] = {"owner": "another-user"}
        with TestClient(harness_server.app, base_url="http://127.0.0.1:8785") as client:
            task = client.get("/api/reviewer/api/paper/upload_a/task/0")
            anchor = client.get("/api/reviewer/api/paper/upload_a/anchor/0")
            saved = client.post(
                "/api/reviewer/api/paper/upload_a/task/0/save",
                headers={"Origin": "http://127.0.0.1:8785"},
                json={"data": {"selected_examples": []}},
            )
            cross_site = client.post(
                "/api/reviewer/api/paper/upload_a/task/0/save",
                headers={"Origin": "https://attacker.invalid"},
                json={"data": {}},
            )
            cross_owner = client.get(
                "/api/reviewer/api/paper/upload_other/task/0",
            )
            collection_index = client.get("/api/reviewer/api/papers")
            gt_bug_bypass = client.post(
                "/api/reviewer/api/gt_bug_flag/upload_other/0",
                json={"flagged": True},
            )
        assert task.status_code == 200
        assert task.json()["data"]["selected_examples"] == []
        assert anchor.status_code == 200
        assert anchor.headers["content-type"].startswith("image/png")
        assert anchor.content == b"png-bytes"
        assert saved.status_code == 200
        assert saved.json()["saved"] is True
        assert cross_site.status_code == 403
        assert cross_owner.status_code == 403
        assert collection_index.status_code == 404
        assert gt_bug_bypass.status_code == 404
        assert [request["method"] for request in ReviewerStub.requests] == [
            "GET",
            "GET",
            "POST",
        ]
    finally:
        if harness_server is not None:
            for job_id, prior in previous_jobs.items():
                if prior is None:
                    harness_server.JOBS.pop(job_id, None)
                else:
                    harness_server.JOBS[job_id] = prior
            harness_server.LOCAL_RUNTIME_USER_ID = previous_local_owner
        reviewer.shutdown()
        reviewer.server_close()
        thread.join(timeout=2)


def test_web_extraction_streams_input_and_keeps_model_key_server_side(tmp_path):
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    os.environ["CHEMRXN_PI_RUNTIME_BASE"] = f"http://127.0.0.1:{runtime.server_port}"
    os.environ["CHEMRXN_PI_INTERNAL_TOKEN"] = token
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = os.environ["CHEMRXN_PI_RUNTIME_BASE"]
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = token
        harness_server.LOCAL_RUNTIME_USER_ID = "local-web-user"
        with TestClient(harness_server.app) as client:
            response = client.post(
                "/api/runtime/extractions",
                data={
                    "source_type": "figure",
                    "auth_mode": "api_key",
                    "llm_provider": "byo-openai",
                    "llm_model": "gpt-4o",
                    "thinking_level": "high",
                    "llm_key": "super-secret-model-key",
                },
                files={"primary": ("figure.png", b"not-real-image", "image/png")},
            )
        assert response.status_code == 201, response.text
        assert response.json()["sessionId"] == "live-session-a"
        assert "super-secret-model-key" not in response.text

        model, upload, create = RuntimeStub.requests
        assert model["path"] == "/v1/models/resolve"
        assert json.loads(model["body"]) == {
            "provider": "openai",
            "modelId": "gpt-4o",
            "requireImage": True,
        }
        assert upload["path"] == "/v1/input-artifacts"
        assert upload["authorization"] == f"Bearer {token}"
        assert upload["owner"] == "local-web-user"
        assert upload["input_kind"] == "figure"
        assert urllib.parse.unquote(upload["file_name"]) == "figure.png"
        assert create["path"] == "/v1/local-agent-sessions"
        request_body = json.loads(create["body"])
        assert request_body["provider"] == "openai"
        assert request_body["modelId"] == "gpt-4o"
        assert request_body["thinkingLevel"] == "high"
        assert request_body["authMode"] == "api_key"
        assert request_body["apiKey"] == "super-secret-model-key"
        assert request_body["startExtraction"] is True
        assert request_body["inputs"] == [{
            "artifactId": "input-artifact-a",
            "kind": "figure",
        }]
    finally:
        if "harness_server" in locals():
            harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_browser_model_preflight_reaches_pi_runtime_without_source_or_credential():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = "internal-test-token"
        harness_server.LOCAL_RUNTIME_USER_ID = "local-preflight-user"
        with TestClient(harness_server.app) as client:
            response = client.post(
                "/api/runtime/models/resolve",
                json={
                    "provider": "byo-gemini",
                    "modelId": "gemini-3.1-pro-preview",
                    "requireImage": True,
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["provider"] == "google"
        assert len(RuntimeStub.requests) == 1
        request = RuntimeStub.requests[0]
        assert request["path"] == "/v1/models/resolve"
        assert request["owner"] == "local-preflight-user"
        assert json.loads(request["body"]) == {
            "provider": "google",
            "modelId": "gemini-3.1-pro-preview",
            "requireImage": True,
        }
        assert "apiKey" not in request["body"]
        assert "source" not in request["body"]
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_product_api_key_mode_never_falls_back_to_the_legacy_session_vault():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = "internal-test-token"
        harness_server.LOCAL_RUNTIME_USER_ID = "local-key-user"
        with TestClient(harness_server.app) as client:
            response = client.post(
                "/api/runtime/extractions",
                data={
                    "source_type": "figure",
                    "auth_mode": "api_key",
                    "llm_provider": "byo-openai",
                    "llm_model": "gpt-5.6-terra",
                },
                files={"primary": ("figure.png", b"not-real-image", "image/png")},
            )
        assert response.status_code == 400
        assert "never loaded from the legacy session vault" in response.json()["detail"]
        assert RuntimeStub.requests == []
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_web_extraction_rejects_invalid_thinking_before_auth_or_input_upload():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = "internal-test-token"
        harness_server.LOCAL_RUNTIME_USER_ID = "local-thinking-user"
        with TestClient(harness_server.app) as client:
            response = client.post(
                "/api/runtime/extractions",
                data={
                    "source_type": "figure",
                    "auth_mode": "pi_auth",
                    "thinking_level": "automatic",
                },
                files={"primary": ("private.png", b"private-input", "image/png")},
            )
        assert response.status_code == 400
        assert "thinking_level must be" in response.json()["detail"]
        assert RuntimeStub.requests == []
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_web_extraction_rejects_an_unknown_pi_model_before_uploading_input():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = "internal-test-token"
        harness_server.LOCAL_RUNTIME_USER_ID = "local-model-user"
        with TestClient(harness_server.app) as client:
            response = client.post(
                "/api/runtime/extractions",
                data={
                    "source_type": "figure",
                    "auth_mode": "api_key",
                    "llm_provider": "byo-openai",
                    "llm_model": "missing-model",
                    "llm_key": "one-request-only-key",
                },
                files={"primary": ("private-figure.png", b"private-input", "image/png")},
            )
        assert response.status_code == 409
        assert "not available in the locked Pi SDK" in response.json()["detail"]
        assert [request["path"] for request in RuntimeStub.requests] == ["/v1/models/resolve"]
        assert "one-request-only-key" not in RuntimeStub.requests[0]["body"]
        assert "private-input" not in RuntimeStub.requests[0]["body"]
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_web_extraction_rejects_malformed_nonempty_models_without_default_fallback():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = "internal-test-token"
        harness_server.LOCAL_RUNTIME_USER_ID = "local-malformed-model-user"
        with TestClient(harness_server.app) as client:
            for malformed_model in ("model with spaces", "m" * 81):
                RuntimeStub.requests = []
                response = client.post(
                    "/api/runtime/extractions",
                    data={
                        "source_type": "figure",
                        "auth_mode": "api_key",
                        "llm_provider": "byo-openai",
                        "llm_model": malformed_model,
                        "llm_key": "one-request-only-key",
                    },
                    files={"primary": ("private-figure.png", b"private-input", "image/png")},
                )
                assert response.status_code == 400
                assert "Model identifiers" in response.json()["detail"]
                assert RuntimeStub.requests == []
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_live_evidence_proxy_serves_tenant_authorized_input_and_allowlisted_artifact():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = "internal-test-token"
        harness_server.LOCAL_RUNTIME_USER_ID = "local-evidence-user"
        with TestClient(harness_server.app) as client:
            source = client.get("/api/runtime/input-artifacts/input-a")
            overview = client.get(
                "/api/runtime/sessions/session-a/live-artifacts/overview"
            )
            pdf_page = client.get(
                "/api/runtime/sessions/session-a/live-artifacts/pdf-main-page-1"
            )
            checkpoint = client.get(
                f"/api/runtime/sessions/session-a/live-artifacts/pdf-checkpoint-{'c' * 64}"
            )
            denied = client.get(
                "/api/runtime/sessions/session-a/live-artifacts/arbitrary-path"
            )
        assert source.status_code == 200
        assert source.content == b"immutable-source"
        assert source.headers["cache-control"] == "private, no-store"
        assert overview.status_code == 200
        assert overview.content == b"live-overview"
        assert pdf_page.status_code == 200
        assert pdf_page.content == b"live-pdf-page"
        assert checkpoint.status_code == 200
        assert checkpoint.content == b"immutable-pdf-checkpoint"
        assert denied.status_code == 404
        proxied = [item for item in RuntimeStub.requests if item["path"] != "/healthz"]
        assert all(item["authorization"] == "Bearer internal-test-token" for item in proxied)
        assert all(item["owner"] == "local-evidence-user" for item in proxied)
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_web_extraction_uses_isolated_pi_auth_without_sending_a_key():
    RuntimeStub.requests = []
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = token
        harness_server.LOCAL_RUNTIME_USER_ID = "local-pi-user"
        with TestClient(harness_server.app) as client:
            response = client.post(
                "/api/runtime/extractions",
                data={"source_type": "figure", "auth_mode": "pi_auth"},
                files={"primary": ("figure.png", b"not-real-image", "image/png")},
            )
        assert response.status_code == 201, response.text
        auth, model, upload, create = RuntimeStub.requests
        assert auth["path"] == "/v1/auth/status"
        assert model["path"] == "/v1/models/resolve"
        assert json.loads(model["body"]) == {
            "provider": "openai-codex",
            "modelId": "gpt-5.6-sol",
            "requireImage": True,
        }
        assert upload["path"] == "/v1/input-artifacts"
        request_body = json.loads(create["body"])
        assert request_body["authMode"] == "pi_auth"
        assert request_body["provider"] == "openai-codex"
        assert request_body["modelId"] == "gpt-5.6-sol"
        assert request_body["thinkingLevel"] == "medium"
        assert "apiKey" not in request_body
    finally:
        harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_live_figure_handoff_preserves_prediction_bytes_and_reuses_reviewer(tmp_path):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
        "AScY42YAAAAASUVORK5CYII="
    )
    prediction = (
        b'{"selected_examples":[{"identifier":"1a","reactants":[{"smiles":"CCO"}],'
        b'"products":[{"smiles":"CC=O"}],"conditions":[],"metrics":[]}]}'
    )
    molparser = b'[{"bbox":[0,0,1,1],"smiles":"CCO","bbox_index":0}]'
    artifact_bytes = {
        "artifact:input": png,
        "artifact:prediction": prediction,
        "artifact:molparser": molparser,
        "artifact:overview": png,
    }
    artifacts = [
        {
            "id": artifact_id,
            "kind": kind,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mediaType": media_type,
            "locator": locator,
        }
        for artifact_id, kind, payload, media_type, locator in [
            ("artifact:input", "input_figure", png, "image/png", "input/figure.png"),
            ("artifact:prediction", "prediction_v2", prediction, "application/json", "output/result.v2.json"),
            ("artifact:molparser", "molparser_raw", molparser, "application/json", "output/perception/raw.json"),
            ("artifact:overview", "overview_render", png, "image/png", "output/perception/overview.png"),
        ]
    ]
    live_result = {
        "schemaVersion": "chemrxn.runtime-result.v1",
        "sessionId": "live-review",
        "jobId": "web-extract:test",
        "contextMode": "figure",
        "source": {
            "mode": "live_pi_sdk",
            "reconstructed": False,
            "liveEventCapture": True,
            "inputArtifactIds": ["input-a"],
            "workspaceId": "live-review",
        },
        "domainContract": "chemrxn.figure-job.v1",
        "terminalStatus": "review_ready",
        "gtIncluded": False,
        "viewer": {
            "kind": "figure",
            "inputArtifactId": "artifact:input",
            "predictionArtifactId": "artifact:prediction",
            "overviewArtifactId": "artifact:overview",
        },
        "manifest": {
            "model": {"provider": "openai", "id": "gpt-4o"},
            "cache": {"molparser": {"detectionCount": 1}},
            "artifacts": artifacts,
        },
    }

    class ReviewerRuntimeStub(RuntimeStub):
        def do_GET(self):
            path = urllib.parse.unquote(self.path)
            if path.endswith("/result"):
                body = json.dumps(live_result, separators=(",", ":")).encode()
            elif "/artifacts/" in path:
                artifact_id = path.split("/artifacts/", 1)[1]
                body = artifact_bytes[artifact_id]
            else:
                return super().do_GET()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json" if path.endswith("/result") or artifact_id.endswith(("prediction", "molparser"))
                else "image/png",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    runtime = ThreadingHTTPServer(("127.0.0.1", 0), ReviewerRuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    token = "internal-test-token"
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = token
        harness_server.LOCAL_RUNTIME_USER_ID = "local-review-user"
        harness_server.JOBS_DIR = tmp_path / "jobs"
        harness_server.PDF_ROOT = tmp_path / "reviewer"
        harness_server.JOBS.clear()
        with TestClient(harness_server.app) as client:
            response = client.post("/api/runtime/sessions/live-review/reviewer")
        assert response.status_code == 200, response.text
        job_id = response.json()["jobId"]
        job_dir = harness_server.JOBS_DIR / job_id
        assert job_dir.joinpath("result.json").read_bytes() == prediction
        assert job_dir.joinpath("work/0_molparser.json").read_bytes() == molparser
        assert job_dir.joinpath("work/0_annotated.png").read_bytes() == png
        slug = f"upload_{job_id}"
        assert (
            harness_server.PDF_ROOT / slug / "0_molparser.json"
        ).read_bytes() == molparser
        reviewer_prediction = json.loads(
            (harness_server.PDF_ROOT / slug / "0_predicted_output.json").read_text()
        )
        assert reviewer_prediction["selected_examples"][0]["identifier"] == "1a"
        assert reviewer_prediction["extractor"] == "Pi SDK reaction-extract (openai/gpt-4o)"
        assert response.json()["sourcePredictionSha256"] == hashlib.sha256(prediction).hexdigest()
        assert "runtime_session=live-review" in response.json()["workspaceUrl"]
    finally:
        if "harness_server" in locals():
            harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)


def test_live_pdf_handoff_preserves_prediction_and_view_bytes_in_document_mode(tmp_path):
    prediction = json.dumps({
        "schema_version": "rxnharbor-pred-v2",
        "coordinate_space": "pdf_points_v1",
        "paper_id": "live",
        "anchors": [{
            "anchor_figure": "Scheme 1",
            "anchor_page": 1,
            "selected_examples": [{
                "instance_id": "1a",
                "reactants": [{
                    "identifier": "1",
                    "E-SMILES": "CCO",
                    "_source": {
                        "origin": "image",
                        "type": "image_structure",
                        "bbox_page": 1,
                        "bbox_index": 0,
                        "md5": "a" * 32,
                    },
                }, {
                    "identifier": "SI-supported starting material",
                    "E-SMILES": "CCN",
                    "_source": {
                        "origin": "derived",
                        "type": "inferred_chemistry",
                        "source_document": "main",
                        "src": "main",
                        "derivation": {
                            "method": "main_markush_plus_si_identifier_resolution",
                            "identifier": "SI-supported starting material",
                            "tool": "fhr.reverse_assemble",
                            "audit_file": "work/fhr_scope_audit.json",
                            "cross_document_confirmation": {
                                "source_document": "si",
                                "page": 2,
                                "md5": "b" * 32,
                            },
                            "main_label_evidence": {
                                "origin": "image",
                                "type": "image_ocr",
                                "bbox": [10, 20, 30, 40],
                                "bbox_page": 1,
                                "bbox_frame": "pdf_points",
                                "source_document": "main",
                                "src": "main",
                            },
                        },
                    },
                }],
                "products": [{"identifier": "1a", "E-SMILES": "CC=O"}],
                "conditions": [],
                "metrics": [],
            }],
        }, {
            "anchor_figure": "SI Scheme",
            "anchor_page": 2,
            "source_document": "si",
            "selected_examples": [{
                "instance_id": "si-1",
                "reactants": [{
                    "identifier": "SI-1",
                    "E-SMILES": "CCN",
                    "_source": {
                        "origin": "image",
                        "type": "image_structure",
                        "bbox_page": 2,
                        "bbox_index": 0,
                        "md5": "b" * 32,
                    },
                }],
                "products": [{"identifier": "SI-2", "E-SMILES": "CC=N"}],
                "conditions": [],
                "metrics": [],
            }],
        }],
    }, separators=(",", ":")).encode()
    pdf_view = json.dumps({
        "pages": [{"page": 1, "width": 300, "height": 200}],
        "anchors": [{
            "index": 0,
            "label": "Scheme 1",
            "page": 1,
            "bbox": [10, 10, 290, 190],
            "n_reactions": 1,
            "source_document": "main",
        }, {
            "index": 1,
            "label": "SI Scheme",
            "page": 2,
            "bbox": [20, 20, 380, 580],
            "n_reactions": 1,
            "source_document": "si",
        }],
        "molecules": [{
            "anchor": 1,
            "reaction": 0,
            "side": "reactants",
            "mol_index": 0,
            "identifier": "SI-1",
            "page": 2,
            "bbox": [40, 60, 120, 140],
            "source": "pred",
            "source_document": "si",
        }],
        "stats": {"n_reactions": 2},
    }, separators=(",", ":")).encode()
    import fitz

    main_document = fitz.open()
    main_document.new_page(width=300, height=200).insert_text((24, 36), "Main paper")
    pdf = main_document.tobytes()
    main_document.close()
    si_document = fitz.open()
    si_document.new_page(width=400, height=600).insert_text((24, 36), "Supporting information page 1")
    si_document.new_page(width=400, height=600).insert_text((24, 36), "Supporting information page 2")
    si_pdf = si_document.tobytes()
    si_document.close()
    validation = b"VALID -- live"
    raw_molparser = json.dumps([{
        "bbox_index": 0,
        "bbox": [40, 60, 120, 140],
        "smi": "CCO",
        "md5": "a" * 32,
        "score": 0.99,
    }], separators=(",", ":")).encode()
    raw_si_molparser = json.dumps([{
        "bbox_index": 0,
        "bbox": [80, 120, 240, 280],
        "smi": "CCN",
        "md5": "b" * 32,
        "score": 0.98,
    }], separators=(",", ":")).encode()
    artifact_bytes = {
        "artifact:pdf": pdf,
        "artifact:si": si_pdf,
        "artifact:prediction": prediction,
        "artifact:view": pdf_view,
        "artifact:validation": validation,
        "artifact:raw-main-p1": raw_molparser,
        "artifact:raw-si-p2": raw_si_molparser,
    }
    artifacts = [
        {
            "id": key,
            "kind": kind,
            "sha256": hashlib.sha256(value).hexdigest(),
            "mediaType": media_type,
            "locator": locator,
        }
        for key, kind, value, media_type, locator in [
            ("artifact:pdf", "main_pdf", pdf, "application/pdf", "input/main.pdf"),
            ("artifact:si", "si_pdf", si_pdf, "application/pdf", "input/si.pdf"),
            ("artifact:prediction", "prediction", prediction, "application/json", "output/result.json"),
            ("artifact:view", "pdf_view", pdf_view, "application/json", "output/pdf_view.json"),
            ("artifact:validation", "validation", validation, "text/plain", "output/validation.txt"),
            (
                "artifact:raw-main-p1",
                "raw_perception",
                raw_molparser,
                "application/json",
                "perception/main/pages/page_001_molparser.json",
            ),
            (
                "artifact:raw-si-p2",
                "raw_perception",
                raw_si_molparser,
                "application/json",
                "perception/si/pages/page_002_molparser.json",
            ),
        ]
    ]
    next(
        artifact for artifact in artifacts
        if artifact["id"] == "artifact:raw-si-p2"
    ).pop("locator")
    live_result = {
        "schemaVersion": "chemrxn.runtime-result.v1",
        "source": {"mode": "live_pi_sdk"},
        "domainContract": "chemrxn.pdf-replay.v1",
        "terminalStatus": "review_ready",
        "gtIncluded": False,
        "viewer": {
            "kind": "document",
            "inputArtifactId": "artifact:pdf",
            "predictionArtifactId": "artifact:prediction",
            "overviewArtifactId": "artifact:view",
        },
        "manifest": {
            "versions": {"modelProvider": "openai-codex", "modelId": "gpt-5.5"},
            "documents": [
                {
                    "id": "doc:main:test",
                    "sourceDocument": "main",
                    "artifactId": "artifact:pdf",
                    "pageCount": 1,
                },
                {
                    "id": "doc:si:test",
                    "sourceDocument": "si",
                    "artifactId": "artifact:si",
                    "pageCount": 2,
                },
            ],
            "expected": {
                "pageCount": 1,
                "anchorCount": 2,
                "reactionCount": 2,
                "validationArtifactId": "artifact:validation",
            },
            "lineage": [{
                "sourceDocument": "si",
                "pageId": "doc:si:test:p0002",
                "rawPerception": {"artifactId": "artifact:raw-si-p2"},
            }],
            "artifacts": artifacts,
        },
    }

    class PdfReviewerRuntimeStub(RuntimeStub):
        def do_GET(self):
            request_path = urllib.parse.unquote(self.path)
            if request_path.endswith("/result"):
                body = json.dumps(live_result, separators=(",", ":")).encode()
            elif "/artifacts/" in request_path:
                artifact_id = request_path.split("/artifacts/", 1)[1]
                body = artifact_bytes.get(artifact_id)
                if body is None:
                    self.send_error(404)
                    return
            else:
                return super().do_GET()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    runtime = ThreadingHTTPServer(("127.0.0.1", 0), PdfReviewerRuntimeStub)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        from harness.service import harness_server

        harness_server.PI_RUNTIME_BASE = f"http://127.0.0.1:{runtime.server_port}"
        harness_server.PI_RUNTIME_INTERNAL_TOKEN = "internal-test-token"
        harness_server.LOCAL_RUNTIME_USER_ID = "local-review-user"
        harness_server.JOBS_DIR = tmp_path / "jobs"
        harness_server.PDF_ROOT = tmp_path / "reviewer"
        harness_server.JOBS.clear()
        with TestClient(harness_server.app) as client:
            response = client.post("/api/runtime/sessions/live-pdf/reviewer")
        assert response.status_code == 200, response.text
        job_id = response.json()["jobId"]
        job_dir = harness_server.JOBS_DIR / job_id
        assert job_dir.joinpath("result.json").read_bytes() == prediction
        assert job_dir.joinpath("work/pdf_view.json").read_bytes() == pdf_view
        assert job_dir.joinpath("work/main.pdf").read_bytes() == pdf
        assert job_dir.joinpath("work/si.pdf").read_bytes() == si_pdf
        evidence = json.loads(job_dir.joinpath("work/reviewer_v2.json").read_text())
        assert evidence["schema"] == "pdf_reviewer_v2_evidence_v1"
        assert evidence["paper_id"] == "live"
        assert len(evidence["rows"]) == 2
        assert evidence["rows"][0]["id"] == "pred:0:0"
        assert evidence["rows"][0]["source_document"] == "main"
        reactant_evidence = evidence["rows"][0]["reactants"][0]
        assert reactant_evidence["source"]["class"] == "molparser_raw"
        assert reactant_evidence["raw_molparser"]["md5"] == "a" * 32
        assert reactant_evidence["raw_molparser"]["bbox_index"] == 0
        cross_document_evidence = evidence["rows"][0]["reactants"][1]
        assert cross_document_evidence["source"]["class"] == "identifier_resolution"
        assert cross_document_evidence["raw_molparser"]["md5"] == "b" * 32
        assert cross_document_evidence["raw_molparser"]["source_document"] == "si"
        assert cross_document_evidence["raw_molparser"]["namespace"] == "page"
        assert cross_document_evidence["raw_molparser"]["owner"] == "2"
        assert cross_document_evidence["raw_molparser"]["bbox_index"] == 0
        assert cross_document_evidence["source"]["derivation_status"] == {
            "audit_file": "work/fhr_scope_audit.json",
            "supporting_document": "si",
            "supporting_page": 2,
        }
        assert cross_document_evidence["bbox"] is None
        assert cross_document_evidence["bbox_page"] is None
        assert cross_document_evidence["geometry_source"] is None
        assert evidence["rows"][1]["id"] == "pred:1:0"
        assert evidence["rows"][1]["source_document"] == "si"
        si_reactant_evidence = evidence["rows"][1]["reactants"][0]
        assert si_reactant_evidence["source"]["class"] == "molparser_raw"
        assert si_reactant_evidence["raw_molparser"]["md5"] == "b" * 32
        assert job_dir.joinpath(
            "work/perception/main/pages/page_001_molparser.json",
        ).read_bytes() == raw_molparser
        assert job_dir.joinpath(
            "work/perception/si/pages/page_002_molparser.json",
        ).read_bytes() == raw_si_molparser
        assert json.loads(job_dir.joinpath("meta.json").read_text())["mode"] == "pdf"
        assert response.json()["sourcePdfViewSha256"] == hashlib.sha256(pdf_view).hexdigest()
        assert f"job={job_id}" in response.json()["workspaceUrl"]
        with TestClient(harness_server.app) as client:
            meta_response = client.get(f"/api/jobs/{job_id}/pdf/meta")
            evidence_response = client.get(f"/api/jobs/{job_id}/pdf/evidence")
            si_page_response = client.get(
                f"/api/jobs/{job_id}/pdf/page/2?source=si&dpi=96",
            )
            si_file_response = client.get(f"/api/jobs/{job_id}/pdf/file?source=si")
            si_anchor_response = client.get(f"/api/jobs/{job_id}/pdf/anchor/1")
            si_crop_response = client.get(f"/api/jobs/{job_id}/pdf/anchor/1/crop?dpi=96")
            invalid_source_response = client.get(
                f"/api/jobs/{job_id}/pdf/page/1?source=other",
            )
            si_geometry_preview = client.post(
                f"/api/jobs/{job_id}/pdf/review/preview",
                json={
                    "operation": "locate_pdf_geometry",
                    "base_version": 0,
                    "target": {
                        "difference_id": "pred:1:0",
                        "target_type": "molecule",
                        "target_id": si_reactant_evidence[
                            "occurrence"
                        ]["occurrence_id"],
                        "target_path": ["reactants", 0],
                    },
                    "after": {
                        "source_document": "si",
                        "page": 2,
                        "bbox_frame": "pdf_points",
                        "bbox": [50, 70, 130, 150],
                    },
                    "note": "runtime SI geometry",
                },
            )
        assert meta_response.status_code == 200
        assert [
            (item["source"], item["pageCount"])
            for item in meta_response.json()["documents"]
        ] == [("main", 1), ("si", 2)]
        si_meta = meta_response.json()["documents"][1]
        assert si_meta["coordinateFrame"] == "pdf_points"
        assert si_meta["coordinateDpi"] == 72
        assert si_meta["pages"][0] == {"page": 1, "width": 400.0, "height": 600.0}
        assert meta_response.json()["reviewer_v2_available"] is True
        assert evidence_response.status_code == 200
        assert evidence_response.json() == evidence
        assert si_page_response.status_code == 200
        assert si_page_response.headers["content-type"].startswith("image/png")
        assert si_file_response.status_code == 200
        assert si_file_response.content == si_pdf
        assert si_anchor_response.status_code == 200
        assert si_anchor_response.json()["view"]["source_document"] == "si"
        assert si_anchor_response.json()["molecules"][0]["identifier"] == "SI-1"
        assert si_crop_response.status_code == 200
        assert si_crop_response.headers["content-type"].startswith("image/png")
        assert invalid_source_response.status_code == 400
        assert si_geometry_preview.status_code == 200, si_geometry_preview.text
        assert si_geometry_preview.json()["after"]["original"] == {
            "source_document": "si",
            "page": 2,
            "bbox_frame": "pdf_points",
            "bbox": [40.0, 60.0, 120.0, 140.0],
        }
        prediction_before = job_dir.joinpath("result.json").read_bytes()
        viewer_before = job_dir.joinpath("work/pdf_view.json").read_bytes()
        job_dir.joinpath("work/reviewer_v2.json").unlink()
        with TestClient(harness_server.app) as client:
            rebuilt_response = client.get(f"/api/jobs/{job_id}/pdf/evidence")
        assert rebuilt_response.status_code == 200
        assert rebuilt_response.json() == evidence
        assert job_dir.joinpath("result.json").read_bytes() == prediction_before
        assert job_dir.joinpath("work/pdf_view.json").read_bytes() == viewer_before

        # A sealed replay may use the same handoff only when the runtime
        # explicitly published its immutable viewer artifact contract.
        live_result["source"] = {
            "mode": "sealed_replay",
            "fixture": "pdf-example-job",
            "reconstructed": True,
            "liveEventCapture": False,
        }
        for artifact in artifacts:
            if artifact["kind"] == "raw_perception":
                artifact["availability"] = "external"
                artifact_bytes.pop(artifact["id"])
        with TestClient(harness_server.app) as client:
            sealed_response = client.post("/api/runtime/sessions/sealed-pdf/reviewer")
        assert sealed_response.status_code == 200, sealed_response.text
        sealed_job = harness_server.JOBS_DIR / sealed_response.json()["jobId"]
        assert sealed_job.joinpath("result.json").read_bytes() == prediction
        assert sealed_job.joinpath("work/pdf_view.json").read_bytes() == pdf_view
        assert json.loads(sealed_job.joinpath("meta.json").read_text())["tag"] == "sealed-demo-pdf"
        sealed_evidence = json.loads(
            sealed_job.joinpath("work/reviewer_v2.json").read_text()
        )
        assert sealed_evidence["rows"][0]["reactants"][0]["source"]["class"] != "molparser_raw"
        assert sealed_evidence["rows"][1]["reactants"][0]["source"]["class"] != "molparser_raw"
        sealed_job_id = sealed_response.json()["jobId"]
        source_before = (
            sealed_job.joinpath("result.json").read_bytes(),
            sealed_job.joinpath("work/pdf_view.json").read_bytes(),
        )
        correction = {
            "difference_id": "pred:0:0",
            "verdict": "mark uncertain",
            "note": "verify against the source document",
            "correction": None,
            "updated_at": 1.0,
        }
        with TestClient(harness_server.app) as client:
            initial = client.get(f"/api/jobs/{sealed_job_id}/pdf/review/versions")
            preview = client.post(
                f"/api/jobs/{sealed_job_id}/pdf/review/preview",
                json={"base_version": 0, "after": correction, "proposed_at": "2026-07-26T00:00:00Z"},
            )
        assert initial.status_code == 200
        assert initial.json()["head_version"] == 0
        assert preview.status_code == 200, preview.text
        assert preview.json()["source"] == initial.json()["source"]
        assert not sealed_job.joinpath("review").exists()

        # A proposal is bound to the immutable reviewer sources. Even if an
        # out-of-contract writer changes a source between Preview and Apply,
        # Apply must fail before creating a version or advancing the head.
        sealed_job.joinpath("result.json").write_bytes(source_before[0] + b"\n")
        with TestClient(harness_server.app) as client:
            stale = client.post(
                f"/api/jobs/{sealed_job_id}/pdf/review/apply",
                json={"proposal": preview.json()},
            )
        assert stale.status_code == 409, stale.text
        assert not sealed_job.joinpath("review").exists()
        sealed_job.joinpath("result.json").write_bytes(source_before[0])

        with TestClient(harness_server.app) as client:
            applied = client.post(
                f"/api/jobs/{sealed_job_id}/pdf/review/apply",
                json={
                    "proposal": preview.json(),
                    "agent_proposal_id": "agent-proposal-pdf-1",
                },
            )
        assert applied.status_code == 200, applied.text
        assert applied.json()["head_version"] == 1
        assert applied.json()["adjudications"][0]["verdict"] == "mark uncertain"
        assert sealed_job.joinpath("review/reviewed-v1.json").is_file()
        reviewed_snapshot = json.loads(
            sealed_job.joinpath("review/reviewed-v1.json").read_text(),
        )
        assert reviewed_snapshot["agent_proposal_id"] == "agent-proposal-pdf-1"
        assert sealed_job.joinpath("review/diff-v1.json").is_file()
        assert source_before == (
            sealed_job.joinpath("result.json").read_bytes(),
            sealed_job.joinpath("work/pdf_view.json").read_bytes(),
        )

        with TestClient(harness_server.app) as client:
            undone = client.post(
                f"/api/jobs/{sealed_job_id}/pdf/review/undo",
                json={"head_version": 1},
            )
        assert undone.status_code == 200, undone.text
        assert undone.json()["head_version"] == 0
        assert undone.json()["adjudications"] == []
        assert sealed_job.joinpath("review/reviewed-v1.json").is_file()
    finally:
        if "harness_server" in locals():
            harness_server.LOCAL_RUNTIME_USER_ID = ""
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=2)
