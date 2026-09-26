import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "reviewer" / "vendor" / "ketcher"
MODAL = ROOT / "reviewer" / "chem_reviewer" / "web" / "components" / "KetcherModal.js"
ADAPTER = ROOT / "reviewer" / "chem_reviewer" / "web" / "lib" / "esmiles-ketcher.js"


def test_vendored_release_has_pinned_esmiles_runtime():
    manifest = json.loads((VENDOR / "asset-manifest.json").read_text())
    main_js = VENDOR / manifest["files"]["main.js"].lstrip("/")
    assert main_js.is_file()
    source = main_js.read_text(errors="ignore")
    assert source.count("getEsmiles") >= 3
    assert "<sep>" in source

    media = VENDOR / "static" / "media"
    assert list(media.glob("RDKit_minimal.*.wasm"))
    assert list(media.glob("indigo-ketcher-1.44.8-9-f24a62cf.*.wasm"))


def test_reviewer_passes_and_saves_complete_esmiles():
    modal = MODAL.read_text()
    adapter = ADAPTER.read_text()

    assert 'from "/static/lib/esmiles-ketcher.js"' in modal
    assert "await loadEsmiles(ketcher, initial)" in modal
    assert "await exportEsmiles(ketcher, target?.smiles || \"\")" in modal

    assert "await ketcher.setMolecule(input)" in adapter
    assert "await ketcher.getEsmiles()" in adapter
    assert "assertEsmilesRoundtrip" in adapter

    combined = modal + adapter
    assert 'split("<sep>")[0]' not in combined
    assert "ketcher.getSmiles()" not in combined
