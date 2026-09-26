import hashlib
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/skills/annotation-review/scripts/snapshot_acceptance.py"


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table acceptance_pages (
          id integer primary key, batch_id text, page_id text, origin_team text,
          origin_lead text, state text, revision_id integer, round integer,
          created_at text, updated_at text, archived_at text, archived_by text
        );
        create table revisions (
          id integer primary key, task_id integer, revision_number integer,
          payload_json text, payload_sha256 text, submitted_by text, submitted_at text
        );
        create table tasks (
          id integer primary key, page_id text, current_revision_id integer,
          approved_revision_id integer
        );
        create table acceptance_molecules (page_row_id integer, molecule_key text);
        create table cross_reviews (id integer primary key, page_row_id integer, round integer);
        create table cross_review_items (review_id integer, molecule_key text);
        create table appeals (id integer primary key, page_row_id integer);
        create table acceptance_audit (id integer primary key, page_row_id integer);
        create table reviews (id integer primary key, task_id integer);
        create table review_items (review_id integer, molecule_key text);
        create table audit_events (id integer primary key, task_id integer);
        """
    )
    payload = '{"version":"accepted"}'
    conn.execute("insert into tasks values (1,'page-a',2,2)")
    conn.execute(
        "insert into revisions values (2,1,1,?,?,?,?)",
        (payload, hashlib.sha256(payload.encode()).hexdigest(), "reviewer", "2026-07-29T00:00:00Z"),
    )
    conn.execute(
        "insert into acceptance_pages values (1,'batch','page-a','A','lead','SETTLED',2,1,?,?,?,?)",
        ("2026-07-29T00:00:00Z", "2026-07-29T01:00:00Z", None, ""),
    )
    conn.execute(
        "insert into acceptance_pages values (2,'batch','page-b','A','lead','CROSS_IN_REVIEW',null,1,?,?,?,?)",
        ("2026-07-29T00:00:00Z", "2026-07-29T01:00:00Z", None, ""),
    )
    conn.commit()
    conn.close()


def test_readonly_acceptance_snapshot_preserves_stages_and_hashes(tmp_path):
    db = tmp_path / "workflow.sqlite3"
    data = tmp_path / "data"
    for name in ("pages", "results", "annotations", "parser_results"):
        (data / name).mkdir(parents=True)
    (data / "pages/page-a.png").write_bytes(b"png")
    (data / "results/page-a.v2.json").write_text('{"version":"raw"}')
    (data / "annotations/page-a.v2.json").write_text('{"version":"working"}')
    (data / "parser_results/page-a.json").write_text('[{"esmi":"C"}]')
    make_db(db)
    before = db.read_bytes()
    out = tmp_path / "snapshot"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db", str(db),
            "--data-root", str(data),
            "--states", "SETTLED",
            "--include-history",
            "--out", str(out),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(proc.stdout)["count"] == 1
    assert db.read_bytes() == before
    assert (out / "pages/page-a/source.png").read_bytes() == b"png"
    assert json.loads((out / "pages/page-a/pre_review_result.v2.json").read_text())["version"] == "raw"
    assert json.loads((out / "pages/page-a/current_annotation.patch.json").read_text())["version"] == "working"
    assert json.loads((out / "pages/page-a/accepted_revision.patch.json").read_text())["version"] == "accepted"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["database_open_mode"] == "read_only"
    assert manifest["count"] == 1
    paths = {item["path"]: item for item in manifest["pages"][0]["files"]}
    assert paths["pages/page-a/source.png"]["sha256"] == hashlib.sha256(b"png").hexdigest()
    assert not (out / "pages/page-b").exists()


def test_snapshot_can_stream_tar_without_server_side_output(tmp_path):
    db = tmp_path / "workflow.sqlite3"
    data = tmp_path / "data"
    for name in ("pages", "results", "annotations", "parser_results"):
        (data / name).mkdir(parents=True)
    (data / "pages/page-a.png").write_bytes(b"png")
    make_db(db)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--data-root", str(data),
         "--states", "SETTLED", "--stream-tar"],
        capture_output=True,
        check=True,
    )
    archive = tmp_path / "snapshot.tar"
    archive.write_bytes(proc.stdout)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert "pages/page-a/source.png" in names
