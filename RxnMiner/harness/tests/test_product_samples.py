import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

import harness_server as HS


def _keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def test_removed_pdf_samples_are_not_advertised_or_served():
    client = TestClient(HS.app)
    listing = client.get("/api/samples").json()
    assert all(item["id"] != "example-job-pdf-si" for item in listing)
    for suffix in ("", "?source=main", "?source=si"):
        assert client.get("/api/samples/example-job-pdf-si" + suffix).status_code == 404


def test_single_source_sample_contract_remains_compatible():
    client = TestClient(HS.app)
    listing = client.get("/api/samples").json()
    sample = next(item for item in listing if item["id"] == "example-job-scheme2")

    assert sample["sourceType"] == "figure"
    assert sample["filename"] == "example-scheme-a.png"
    assert sample["previewUrl"] == "/api/samples/example-job-scheme2"
    assert sample["files"] == [{
        "role": "figure",
        "filename": "example-scheme-a.png",
        "url": "/api/samples/example-job-scheme2",
    }]
    direct = client.get(sample["files"][0]["url"])
    assert direct.status_code == 200
    assert direct.content == Path(HS.SAMPLES[0]["src"]).read_bytes()


def test_private_product_gallery_is_source_only_and_path_confined(tmp_path, monkeypatch):
    root = tmp_path / "samples"
    root.mkdir()
    image = root / "scope.png"
    Image.new("RGB", (40, 30), "white").save(image)
    outside = tmp_path / "outside.png"
    Image.new("RGB", (10, 10), "black").save(outside)
    (root / "manifest.json").write_text(json.dumps({"samples": [
        {
            "id": "v7-scope",
            "title": "V7 dense scope",
            "sub": "Source-only",
            "group": "Scope",
            "tags": ["V7", "L3"],
            "sourceType": "figure",
            "file": "scope.png",
        },
        {"id": "escape", "title": "escape", "sourceType": "figure", "file": "../outside.png"},
        {"id": "leaky", "title": "leaky", "sourceType": "figure", "file": "scope.png", "gt": "answer"},
    ]}))
    monkeypatch.setenv("CHEMRXN_PRODUCT_SAMPLE_ROOT", str(root))

    rows = HS._external_product_samples()
    assert [row["id"] for row in rows] == ["v7-scope"]
    assert rows[0]["src"] == image.resolve()

    client = TestClient(HS.app)
    listing = client.get("/api/samples").json()
    row = next(item for item in listing if item["id"] == "v7-scope")
    assert row["group"] == "Scope"
    assert row["tags"] == ["V7", "L3"]
    assert "src" not in row and "gt" not in row
    assert row["rights"] == {"status": "undeclared", "label": "Local sample"}
    response = client.get(row["previewUrl"])
    assert response.status_code == 200
    assert response.content == image.read_bytes()


def test_private_product_gallery_rejects_symlink(tmp_path, monkeypatch):
    root = tmp_path / "samples"
    root.mkdir()
    outside = tmp_path / "source.png"
    Image.new("RGB", (10, 10), "white").save(outside)
    link = root / "linked.png"
    link.symlink_to(outside)
    (root / "manifest.json").write_text(json.dumps({"samples": [{
        "id": "linked", "title": "Linked", "sourceType": "figure", "file": "linked.png",
    }]}))
    monkeypatch.setenv("CHEMRXN_PRODUCT_SAMPLE_ROOT", str(root))
    assert HS._external_product_samples() == []


def test_paired_sample_requires_exactly_one_main_and_one_si(tmp_path):
    main = tmp_path / "main.pdf"
    si = tmp_path / "si.pdf"
    main.write_bytes(b"%PDF-1.4\n")
    si.write_bytes(b"%PDF-1.4\n")

    assert HS._product_sample_sources({
        "sources": [
            {"role": "main", "filename": "main.pdf", "src": main},
            {"role": "main", "filename": "si.pdf", "src": si},
        ],
    }) == []
    assert HS._product_sample_sources({"sources": []}) == []
    assert HS._product_sample_sources({
        "sources": [
            {"role": "main", "filename": "main.pdf", "src": main},
            {"role": "si", "filename": "../si.pdf", "src": si},
        ],
    }) == []
