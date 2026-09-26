"""Document (PDF) viewer mode — issue #3 test list.

Covers, offline and deterministic (synthetic PDF + synthetic pred; no network, no GT):
  1. a paper prediction registers exactly ONE PDF job (and retires legacy per-anchor cards);
  2. the original pred.json is stored byte-for-byte unchanged;
  3. page geometry + the crop-pixel -> PDF-point conversion (exact affine values);
  4. a layout region with no predicted anchor is represented independently ("not extracted");
  5. PDF page API bounds + dpi clamp + lazy atomic cache;
  6. old per-figure jobs behave as before (export shapes differ per mode);
  7. GT is absent by default; the adapter works with no GT path present (leak sentinel);
  8. frontend JS syntax check (node) + API smoke (Document Mode markup served).
"""
import asyncio
import io
import json
import csv
import hashlib
import subprocess
import shutil
import sys
import time
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

fitz = pytest.importorskip("fitz", reason="document mode needs pymupdf")
import preds_to_viewer as PV  # noqa: E402
import harness_server as HS   # noqa: E402
from fastapi import HTTPException  # noqa: E402

GT_SENTINEL = "GT_SENTINEL_DO_NOT_LEAK_0xDEADBEEF"


# ---------------------------------------------------------------- fixtures
def _make_pdf(path: Path, n_pages=3, w=612, h=792):
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=w, height=h)
        page.insert_text((72, 72), f"synthetic page {i + 1}")
        if i == 2:
            page.insert_text((72, 110), "butane was charged. gave the alcohol.")
    doc.save(str(path))
    doc.close()


def _pred():
    return {
        "schema_version": "rxnharbor-pred-v2",
        "coordinate_space": "pdf_points_v1",
        "paper_id": "50",
        "anchors": [
            {"anchor_kind": "scheme", "anchor_figure": "Scheme 2", "anchor_page": 2,
             "anchor_bbox_pdf": [48.0, 443.0, 554.0, 519.0],
             "identifier_map": {"10": "ethanol"},
             "selected_examples": [
                 {"instance_id": "1",
                  "reactants": [{"identifier": "10", "E-SMILES": "CCO",
                                 "_source": {"origin": "image", "type": "image_structure"}}],
                  "products": [{"identifier": "11", "E-SMILES": "CCN",
                                "_source": {"origin": "image", "type": "image_structure",
                                            "page": 2, "bbox": [100.0, 100.0, 200.0, 150.0]}}],
                  "conditions": [[{"role": "reagent", "text": "NH3",
                                   "_source": {"origin": "image", "type": "image_ocr"}}]],
                  "metrics": [{"role": "yield", "of": "11", "text": "95%",
                               "_source": {"origin": "text", "type": "paper_text_quote",
                                           "text_page": 3,
                                           "text_quote": "the product was obtained in 95% yield."}}]}],
             "reaction_graph": []},
            {"anchor_kind": "prose", "anchor_figure": "Experimental Section", "anchor_page": 3,
             "identifier_map": {},
             "selected_examples": [
                 {"instance_id": "2",
                  "reactants": [{"identifier": "12", "E-SMILES": "CCCC",
                                 "_source": {"origin": "text", "type": "paper_text_quote",
                                             "text_page": 3, "text_quote": "butane was charged."}}],
                  "products": [{"identifier": "13", "E-SMILES": "CCCCO",
                                "_source": {"origin": "text", "type": "paper_text_quote",
                                            "text_page": 3, "text_quote": "gave the alcohol."}}],
                  "conditions": [[]], "metrics": []}],
             "reaction_graph": []},
        ],
    }


@pytest.fixture()
def world(tmp_path):
    """A self-contained document-mode world: pdf + pred + perception + region labels + release
    geometry. Contains a GT file with a sentinel string that must never leak into viewer output."""
    pdf = tmp_path / "50_main.pdf"
    _make_pdf(pdf)

    pred_path = tmp_path / "pred.json"
    pred_path.write_text(json.dumps(_pred(), indent=1), encoding="utf-8")

    percept = tmp_path / "perception"
    percept.mkdir()
    (percept / "layout.json").write_text(json.dumps({
        "source": "50__paper", "segmenter": "pre_ingested", "n_pages": None,
        "regions": [
            {"region_id": "1", "kind": "figure", "page": None, "bbox_pdf_norm": None,
             "label": "figure 1", "caption": "", "category": "figure", "molparser_json": None},
            {"region_id": "2", "kind": "figure", "page": None, "bbox_pdf_norm": None,
             "label": "figure 2", "caption": "", "category": "figure",
             "molparser_json": "2_molparser.json"},
        ]}), encoding="utf-8")
    (percept / "2_molparser.json").write_text(json.dumps([
        {"bbox_index": 0, "bbox": [100, 50, 400, 140], "smi": "CCO", "markush": False, "score": 1.0},
        {"bbox_index": 1, "bbox": [500, 50, 700, 140], "smi": "c1ccccc1", "markush": False, "score": 0.9},
    ]), encoding="utf-8")

    pool = tmp_path / "pool"
    for rid, label in (("1", "Scheme 1"), ("2", "Scheme 2")):
        d = pool / "50__paper" / rid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": f"x__{rid}", "anchor_figure": label}),
                                     encoding="utf-8")
    # GT lives NEXT TO the region meta, as on the server. It must never be opened by the adapter.
    (pool / "50__paper" / "2" / "gt.json").write_text(
        json.dumps({"secret": GT_SENTINEL}), encoding="utf-8")

    release = tmp_path / "release"
    rj = release / "relA" / "50__paper"
    rj.mkdir(parents=True)
    (rj / "50__paper.json").write_text(json.dumps({
        "pdf_name": "50__paper.pdf", "page_count": 3, "export_dpi": 300,
        "coordinate_system": "PDF point; origin = top-left (PyMuPDF convention).",
        "screenshots": [
            {"id": 1, "filename": "1.png", "page": 2, "page_width": 612, "page_height": 792,
             "bbox_pdf": [50.0, 225.0, 500.0, 438.0], "image_size": [1875, 888]},
            {"id": 2, "filename": "2.png", "page": 2, "page_width": 612, "page_height": 792,
             "bbox_pdf": [48.0, 443.0, 554.0, 519.0], "image_size": [1012, 152]},
        ]}), encoding="utf-8")

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    return {"tmp": tmp_path, "pdf": pdf, "pred": pred_path, "percept": percept,
            "pool": pool, "release": release, "jobs": jobs}


def _register(world, collection="docviz", **kw):
    return PV.register_document_job(
        world["pred"], collection, jobs_dir=world["jobs"], pdf_path=world["pdf"],
        perception_dir=world["percept"], paper_pool=world["pool"],
        release_root=world["release"],
        score={"reward": 0.8131, "overall": 0.5245, "B_grounding": 0.9231,
               "C_conditions": 0.087, "D_provenance": 0.3062, "E_struct_dag": 0.3929}, **kw)


def _view(world, job_id):
    return json.loads((world["jobs"] / job_id / "work" / "pdf_view.json").read_text())


def _seal_test_source_rights(job_dir: Path, rights: dict) -> None:
    """Build the server proof fixture; importer generation is tested separately."""
    result_sha = hashlib.sha256((job_dir / "result.json").read_bytes()).hexdigest()
    view_sha = hashlib.sha256(
        (job_dir / "work" / "pdf_view.json").read_bytes(),
    ).hexdigest()
    prediction = json.loads((job_dir / "result.json").read_text())
    paper_id = str(prediction["paper_id"])
    normalized_rights = {
        key: rights.get(key)
        for key in (
            "license",
            "attribution",
            "source_doi",
            "source_url",
            "license_url",
            "change_notice",
        )
    }
    validate_path = job_dir / "work" / "validate.json"
    validation = json.loads(validate_path.read_text()) if validate_path.exists() else {}
    validation.update({
        "schema": "chemrxn.local-handoff-validation.v1",
        "status": "VALID",
        "manifest_declares_gt_free": True,
        "gt_sidecars_mounted": False,
        **normalized_rights,
        "variant": "pdf_only",
        "hashes": {
            "result.json": result_sha,
            "pdf_view.json": view_sha,
        },
    })
    validate_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema": "chemrxn.synthetic-rights-fixture.v1",
        "paper_id": paper_id,
        "anti_leakage": {"gt": False, "reviewed": False, "score": False},
        **rights,
    }
    manifest_path = job_dir / "work" / "handoff_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    source_sha = hashlib.sha256(
        (job_dir / "work" / "main.pdf").read_bytes(),
    ).hexdigest()
    inventory_path = job_dir / "work" / "handoff_SHA256SUMS.txt"
    inventory_path.write_text(
        f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  manifest.json\n"
        f"{result_sha}  pdf_only/result.json\n"
        f"{view_sha}  pdf_only/pdf_view.json\n"
        f"{source_sha}  source/main.pdf\n",
        encoding="utf-8",
    )
    proof = {
        "schema": "chemrxn.source-rights-proof.v1",
        "status": "VALID",
        "job": {
            "job_id": job_dir.name,
            "paper_id": paper_id,
            "variant": "pdf_only",
            "handoff_schema": manifest["schema"],
        },
        "rights": normalized_rights,
        "anti_leakage": {
            "manifest_declares_gt_free": True,
            "gt_sidecars_mounted": False,
        },
        "hashes": {
            "result_json": result_sha,
            "pdf_view_json": view_sha,
            "validation_json": hashlib.sha256(validate_path.read_bytes()).hexdigest(),
            "handoff_manifest": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "handoff_inventory": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
            "source_pdfs": {"main": source_sha},
        },
    }
    (job_dir / "work" / "source_rights.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    meta_path = job_dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta.update({
        **rights,
        "paper_id": paper_id,
        "handoff_variant": "pdf_only",
        "handoff_schema": manifest["schema"],
        "result_sha256": result_sha,
        "pdf_view_sha256": view_sha,
    })
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    HS._SOURCE_RIGHTS_CACHE.clear()


# ---------------------------------------------------------------- 1. one job per paper
def test_registers_exactly_one_document_job(world):
    # pre-seed a legacy per-anchor card: registration must retire it from the collection index
    cdir = world["jobs"] / "_collections"
    cdir.mkdir()
    (cdir / "docviz.jsonl").write_text(json.dumps(
        {"id": "db_docviz_p50__a01__Scheme_2", "filename": "[pi-pdf] paper 50 - Scheme 2",
         "created": 1.0}) + "\n", encoding="utf-8")

    job_id = _register(world)
    assert job_id == "pdf_docviz_p50"
    job_dirs = [d for d in world["jobs"].iterdir() if d.is_dir() and d.name != "_collections"]
    assert [d.name for d in job_dirs] == ["pdf_docviz_p50"]

    rows = [json.loads(x) for x in (cdir / "docviz.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["id"] == "pdf_docviz_p50" and rows[0]["mode"] == "pdf"

    meta = json.loads((world["jobs"] / job_id / "meta.json").read_text())
    assert meta["mode"] == "pdf" and meta["status"] == "done"
    assert meta["n_pages"] == 3 and meta["n_anchors"] == 2 and meta["n_reactions"] == 2
    assert meta["eval_r"] == 0.8131 and meta["pdf_score"]["overall"] == 0.5245
    for k in ("meta.json", "result.json", "input.pdf"):
        assert (world["jobs"] / job_id / k).exists()
    assert (world["jobs"] / job_id / "work" / "main.pdf").exists()
    assert (world["jobs"] / job_id / "work" / "pdf_view.json").exists()
    assert (world["jobs"] / job_id / "work" / "pdf_pages").is_dir()


# ---------------------------------------------------------------- 2. pred unchanged
def test_original_pred_unchanged(world):
    job_id = _register(world)
    assert (world["jobs"] / job_id / "result.json").read_bytes() == world["pred"].read_bytes()


def test_pdf_evidence_preserves_full_esmiles_and_stable_occurrence(world, monkeypatch):
    prediction = _pred()
    tagged = "*c1ccccc1<sep><a>0:R[1]</a>"
    reaction = prediction["anchors"][0]["selected_examples"][0]
    reaction["reactants"][0]["E-SMILES"] = tagged
    world["pred"].write_text(json.dumps(prediction, indent=1), encoding="utf-8")
    raw_before = world["pred"].read_bytes()
    job_id = _register(world, collection="tagged-occurrence")
    job_dir = world["jobs"] / job_id
    evidence = json.loads((job_dir / "work" / "reviewer_v2.json").read_text())
    molecule = evidence["rows"][0]["reactants"][0]
    assert molecule["smi"] == tagged
    assert molecule["occurrence"] == {
        "document_id": "50:main",
        "source_document": "main",
        "page_id": "main:p2",
        "page": 2,
        "anchor_id": "main:a0",
        "anchor": 0,
        "reaction_id": "pred:0:0",
        "reaction": 0,
        "role": "reactant",
        "occurrence_id": "main:a0:r0:reactant:0",
        "molecule_index": 0,
        "molecule_path": [
            "anchors", 0, "selected_examples", 0,
            "reactants", 0, "E-SMILES",
        ],
    }

    monkeypatch.setattr(HS, "JOBS_DIR", world["jobs"])
    correction = {
        **molecule["occurrence"],
        "esmiles": "c1ccccc1*<sep><a>6:R[1]</a>",
    }
    proposal = HS._pdf_review_proposal(job_dir, {
        "base_version": 0,
        "after": {
            "difference_id": "pred:0:0",
            "verdict": "correct fields",
            "note": "explicit one-occurrence Ketcher review",
            "correction": {"molecule": correction},
        },
    })
    assert proposal["after"]["correction"]["molecule"] == correction
    assert proposal["raw_prediction_mutated"] is False
    assert (job_dir / "result.json").read_bytes() == raw_before

    wrong_namespace = json.loads(json.dumps(correction))
    wrong_namespace["source_document"] = "si"
    with pytest.raises(HTTPException, match="source_document"):
        HS._pdf_review_proposal(job_dir, {
            "base_version": 0,
            "after": {
                "difference_id": "pred:0:0",
                "verdict": "correct fields",
                "correction": {"molecule": wrong_namespace},
            },
        })

    lost_extension = {**correction, "esmiles": "c1ccccc1*<sep>"}
    with pytest.raises(HTTPException, match="dropped"):
        HS._pdf_review_proposal(job_dir, {
            "base_version": 0,
            "after": {
                "difference_id": "pred:0:0",
                "verdict": "correct fields",
                "correction": {"molecule": lost_extension},
            },
        })

    changed_label = {
        **correction,
        "esmiles": "c1ccccc1*<sep><a>6:R[2]</a>",
    }
    with pytest.raises(HTTPException, match="labels"):
        HS._pdf_review_proposal(job_dir, {
            "base_version": 0,
            "after": {
                "difference_id": "pred:0:0",
                "verdict": "correct fields",
                "correction": {"molecule": changed_label},
            },
        })

    invalid_coordinate = {
        **correction,
        "esmiles": "c1ccccc1*<sep><a>999:R[1]</a>",
    }
    with pytest.raises(HTTPException, match="structural validation|out of range"):
        HS._pdf_review_proposal(job_dir, {
            "base_version": 0,
            "after": {
                "difference_id": "pred:0:0",
                "verdict": "correct fields",
                "correction": {"molecule": invalid_coordinate},
            },
        })


def test_pdf_evidence_get_enriches_legacy_entity_identity_without_writing(world, monkeypatch):
    job_id = _register(world, collection="legacy-entity-read")
    job_dir = world["jobs"] / job_id
    sidecar = job_dir / "work" / "reviewer_v2.json"
    legacy = json.loads(sidecar.read_text())
    for row in legacy["rows"]:
        for side in ("reactants", "products"):
            for molecule in row[side]:
                molecule.pop("entity_key", None)
                molecule.pop("entity_identity", None)
    sidecar.write_text(json.dumps(legacy, indent=1), encoding="utf-8")
    evidence_before = sidecar.read_bytes()
    result_before = (job_dir / "result.json").read_bytes()
    view_before = (job_dir / "work" / "pdf_view.json").read_bytes()

    monkeypatch.setattr(HS, "JOBS_DIR", world["jobs"])
    response = HS.api_pdf_evidence(job_id)
    molecule = response["rows"][0]["reactants"][0]
    assert molecule["smi"] == "CCO"
    assert molecule["entity_key"].startswith(
        "chem:v1:canonical-isomeric-smiles:sha256:",
    )
    assert molecule["entity_identity"]["canonical_isomeric_smiles"] == "CCO"
    assert sidecar.read_bytes() == evidence_before
    assert (job_dir / "result.json").read_bytes() == result_before
    assert (job_dir / "work" / "pdf_view.json").read_bytes() == view_before


def test_pdf_review_esmiles_validator_rejects_broken_semantic_coordinates():
    valid = [
        "*C<sep><a>0:R[1]</a>",
        "C(O)(=O)*<sep><d>3:<dum></d>",
        "N.O<sep><v>0:A:[0:1]</v>",
        "C(*)(C*)<sep><g>[1:2]:|Sg:n|</g>",
        "c1ccccc1<sep><r>0:R[1]</r>",
        "C1CC1<sep><r>0:<s>*C<sep><a>0:R[1]</a></s></r>",
    ]
    for value in valid:
        HS._validate_pdf_esmiles_structure(value)

    invalid = [
        "*C<sep><a>999:R[1]</a>",
        "CC<sep><a>0:R[1]</a>",
        "CC<sep><d>0:<dum></d>",
        "N.O<sep><v>0:A:[8:9]</v>",
        "C(*)(C*)<sep><g>[8:9]:|Sg:n|</g>",
        "CC<sep><r>0:R[1]</r>",
    ]
    for value in invalid:
        with pytest.raises(HTTPException):
            HS._validate_pdf_esmiles_structure(value)

    assert HS._esmiles_extension_semantics(
        "N.O<sep><v>0:A:[0:1]</v>",
    ) != HS._esmiles_extension_semantics(
        "N.O<sep><v>0:A:[8:9]</v>",
    )
    assert HS._esmiles_unknown_blocks(
        "*C<sep><x>keep</x><a>0:R[1]</a>",
    ) != HS._esmiles_unknown_blocks(
        "*C<sep><x>lost</x><a>0:R[1]</a>",
    )
    assert HS._esmiles_unknown_tags(
        "*C<sep><x value='keep'/><a>0:R[1]</a>",
    ) == ["x"]
    assert HS._esmiles_unknown_tags(
        "*C<sep><d>0:<dum></d><a>0:<id>R1</id></a>",
    ) == []


def test_runtime_pdf_occurrences_keep_main_and_si_namespaces_independent():
    tagged = "*c1ccccc1<sep><a>0:R[1]</a>"
    prediction = _pred()
    main = prediction["anchors"][0]
    main["selected_examples"][0]["reactants"][0]["E-SMILES"] = tagged
    si = json.loads(json.dumps(main))
    si["_source"] = {"source_document": "si"}
    si["anchor_page"] = 7
    prediction["anchors"] = [main, si]
    view = {
        "anchors": [
            {"index": 0, "page": 2, "source_document": "main"},
            {"index": 1, "page": 7, "source_document": "si"},
        ],
    }
    evidence = HS._runtime_pdf_reviewer_data(PV, prediction, view, {})
    assert [row["id"] for row in evidence["rows"]] == ["pred:0:0", "pred:1:0"]
    main_occurrence = evidence["rows"][0]["reactants"][0]["occurrence"]
    si_occurrence = evidence["rows"][1]["reactants"][0]["occurrence"]
    assert main_occurrence["occurrence_id"] == "main:a0:r0:reactant:0"
    assert si_occurrence["occurrence_id"] == "si:a1:r0:reactant:0"
    assert main_occurrence["document_id"] == "50:main"
    assert si_occurrence["document_id"] == "50:si"
    assert main_occurrence["molecule_path"][1] == 0
    assert si_occurrence["molecule_path"][1] == 1
    main_molecule = evidence["rows"][0]["reactants"][0]
    si_molecule = evidence["rows"][1]["reactants"][0]
    assert main_molecule["entity_key"] == si_molecule["entity_key"]
    assert main_occurrence["occurrence_id"] != si_occurrence["occurrence_id"]


# ---------------------------------------------------------------- 3. geometry + conversion
def test_page_geometry_and_crop_to_pdf_conversion(world):
    job_id = _register(world)
    view = _view(world, job_id)

    assert [p["page"] for p in view["pages"]] == [1, 2, 3]
    assert all(p["width"] == 612 and p["height"] == 792 for p in view["pages"])

    # Anchor 1 remains boxless because its reaction gets a separate quote-derived
    # text region rather than a fabricated anchor rectangle.
    a0, a1 = view["anchors"]
    assert a0["page"] == 2 and a0["bbox"] == [48.0, 443.0, 554.0, 519.0] and a0["region_id"] == "2"
    assert a1["bbox"] is None and a1["page"] == 3 and a1["n_reactions"] == 1

    mols = {(m["anchor"], m["side"], m["mol_index"]): m for m in view["molecules"]}
    # precedence 1: the prediction's own _source.bbox is used verbatim ("pred")
    prod = mols[(0, "products", 0)]
    assert prod["source"] == "pred" and prod["page"] == 2
    assert prod["bbox"] == [100.0, 100.0, 200.0, 150.0]
    # precedence 2: unique SMILES match -> crop px -> PDF points through the release transform.
    # region 2: bbox_pdf [48,443,554,519], image_size [1012,152] -> scale = 0.5 pt/px exactly;
    # det [100,50,400,140] -> [48+50, 443+25, 48+200, 443+70] = [98, 468, 248, 513]
    reac = mols[(0, "reactants", 0)]
    assert reac["source"] == "molparser" and reac["page"] == 2
    assert reac["bbox"] == [98.0, 468.0, 248.0, 513.0]
    # never guess: anchor-1 molecules have no bbox, no matched region -> unlocated, no box emitted
    assert (1, "reactants", 0) not in mols and (1, "products", 0) not in mols
    assert view["stats"]["n_molecules_located"] == 2
    assert view["stats"]["n_molecules_unlocated"] == 2

    # text provenance claims carry page + verbatim quote
    quotes = {c["quote"] for c in view["text_claims"]}
    assert "the product was obtained in 95% yield." in quotes
    assert all(c["page"] == 3 for c in view["text_claims"])

    # Every prose occurrence gets an explicit page entry. Exact PDF text produces
    # a restrained line bbox; a missing quote remains an honest fallback marker.
    assert len(view["text_reactions"]) == 1
    text_reaction = view["text_reactions"][0]
    assert text_reaction["anchor"] == 1 and text_reaction["reaction"] == 0
    assert text_reaction["page"] == 3
    assert text_reaction["geometry_source"] == "pdf_text_search"
    assert text_reaction["fallback"] is False
    assert len(text_reaction["bbox"]) == 4
    assert view["stats"]["n_text_reactions"] == 1
    assert view["stats"]["n_text_reactions_located"] == 1


def test_page_molparser_geometry_is_preserved_independently_of_prediction_join(world):
    pages_dir = world["percept"] / "pages"
    pages_dir.mkdir()
    with fitz.open(world["pdf"]) as doc:
        pixmap = doc.load_page(1).get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pixmap.save(str(pages_dir / "page_002.png"))
    (pages_dir / "page_002_molparser.json").write_text(json.dumps([
        # Converts to the prediction product bbox and is deduplicated by the host renderer.
        {"bbox_index": 0, "bbox": [200, 200, 400, 300], "smi": "CCN",
         "md5": "raw-product", "markush": False},
        # Has no prediction identity join but remains independently visible as raw evidence.
        {"bbox_index": 1, "bbox": [20, 20, 60, 60], "smi": "c1ccccc1*",
         "md5": "raw-markush", "markush": True},
    ]), encoding="utf-8")

    view = PV.build_pdf_view(_pred(), world["pdf"], perception_dir=world["percept"])
    raw = view["raw_molparser_detections"]
    assert len(raw) == 2
    assert raw[0]["bbox"] == [100.0, 100.0, 200.0, 150.0]
    assert raw[1]["bbox"] == [10.0, 10.0, 30.0, 30.0]
    assert raw[1]["source"] == "molparser_raw"
    assert raw[1]["namespace"] == "page" and raw[1]["owner"] == "2"
    assert raw[1]["markush"] is True and raw[1]["anchor"] is None
    assert view["stats"]["n_raw_molparser_detections"] == 2
    assert view["stats"]["n_raw_molparser_unlocated"] == 0


def test_missing_prose_quote_uses_page_marker_without_fabricated_bbox(world):
    pred = _pred()
    example = pred["anchors"][1]["selected_examples"][0]
    for molecule in example["products"] + example["reactants"]:
        molecule["_source"]["text_quote"] = "this sentence is not present in the PDF"
    view = PV.build_pdf_view(pred, world["pdf"])
    text_reaction = view["text_reactions"][0]
    assert text_reaction["page"] == 3
    assert text_reaction["bbox"] is None
    assert text_reaction["fallback"] is True
    assert text_reaction["geometry_source"] is None


def test_det_bbox_frame_mismatch_fails_closed(world):
    """A detection bbox that overflows its declared crop frame (the padded-PNG trap) must be
    dropped, not drawn at a wrong place."""
    (world["percept"] / "2_molparser.json").write_text(json.dumps([
        {"bbox_index": 0, "bbox": [100, 50, 400, 400], "smi": "CCO", "markush": False},
    ]), encoding="utf-8")   # y1=400 >> frame height 152
    job_id = _register(world)
    mols = {(m["anchor"], m["side"], m["mol_index"]) for m in _view(world, job_id)["molecules"]}
    assert (0, "reactants", 0) not in mols


# ---------------------------------------------------------------- 4. not-extracted region
def test_missing_layout_region_is_represented(world):
    job_id = _register(world)
    view = _view(world, job_id)
    by_rid = {r["region_id"]: r for r in view["layout"]}
    # Scheme 1 exists in layout with geometry recovered from the release json, but NO anchor matches
    r1 = by_rid["1"]
    assert r1["label"] == "Scheme 1" and r1["matched_anchor"] is None
    assert r1["page"] == 2 and r1["bbox"] == [50.0, 225.0, 500.0, 438.0]
    assert r1["geometry_source"] == "release_meta"
    # Scheme 2 is matched to anchor 0
    assert by_rid["2"]["matched_anchor"] == 0
    assert view["stats"]["n_regions_unextracted"] == 1


# ---------------------------------------------------------------- 5. page API bounds + cache
def test_pdf_page_api_bounds_and_lazy_cache(world, monkeypatch):
    job_id = _register(world)
    monkeypatch.setattr(HS, "JOBS_DIR", world["jobs"])

    meta = HS.api_pdf_meta(job_id)
    assert meta["job"]["mode"] == "pdf" and len(meta["view"]["pages"]) == 3

    r = HS.api_pdf_page(job_id, 2, dpi=144)
    p = Path(r.path)
    assert p.exists() and p.name == "p0002_dpi144.png" and p.stat().st_size > 0
    mtime = p.stat().st_mtime_ns
    r2 = HS.api_pdf_page(job_id, 2, dpi=144)          # cache hit: same file, not re-rendered
    assert Path(r2.path) == p and p.stat().st_mtime_ns == mtime
    assert not list(p.parent.glob("*.tmp"))            # atomic publish leaves no torn temp files

    for bad in (0, -1, 4, 99):
        with pytest.raises(HTTPException) as e:
            HS.api_pdf_page(job_id, bad)
        assert e.value.status_code == 404

    r3 = HS.api_pdf_page(job_id, 1, dpi=99999)         # dpi clamped, not an error
    assert Path(r3.path).name == f"p0001_dpi{HS._PDF_DPI_MAX}.png"
    r4 = HS.api_pdf_page(job_id, 1, dpi=1)
    assert Path(r4.path).name == f"p0001_dpi{HS._PDF_DPI_MIN}.png"

    rf = HS.api_pdf_file(job_id)
    assert Path(rf.path).read_bytes() == world["pdf"].read_bytes()

    a = HS.api_pdf_anchor(job_id, 0)
    assert a["anchor"]["anchor_figure"] == "Scheme 2" and a["view"]["index"] == 0
    assert len(a["molecules"]) == 2
    crop = Path(HS.api_pdf_anchor_crop(job_id, 0, dpi=72, pad=8).path)
    assert crop.exists() and crop.parent.name == "pdf_anchor_crops"
    assert crop.name == "main_a0000_p0002_dpi72_pad80.png"
    crop_mtime = crop.stat().st_mtime_ns
    assert Path(HS.api_pdf_anchor_crop(job_id, 0, dpi=72, pad=8).path) == crop
    assert Path(HS.api_pdf_anchor_crop(job_id, 0, dpi=72, pad=8.04).path) == crop
    assert crop.stat().st_mtime_ns == crop_mtime
    assert not list(crop.parent.glob("*.tmp"))
    for index in range(HS._PAGE_CACHE_MAX_FILES + 2):
        (crop.parent / f"a_fake_{index:04d}.png").touch()
    HS._prune_page_cache(crop.parent)
    assert len(list(crop.parent.glob("*.png"))) == HS._PAGE_CACHE_MAX_FILES
    with pytest.raises(HTTPException):
        HS.api_pdf_anchor(job_id, 2)
    with pytest.raises(HTTPException):
        HS.api_pdf_anchor(job_id, -1)
    with pytest.raises(HTTPException):
        HS.api_pdf_anchor_crop(job_id, 99)
    view_path = world["jobs"] / job_id / "work" / "pdf_view.json"
    unsafe_view = json.loads(view_path.read_text(encoding="utf-8"))
    unsafe_view["anchors"][0]["source_document"] = "../other-job/work/main"
    view_path.write_text(json.dumps(unsafe_view), encoding="utf-8")
    with pytest.raises(HTTPException) as unsafe_source:
        HS.api_pdf_anchor_crop(job_id, 0)
    assert unsafe_source.value.status_code == 409


def _fake_figure_job(jobs: Path, job_id="fig0001"):
    from PIL import Image
    d = jobs / job_id
    (d / "work").mkdir(parents=True)
    Image.new("RGB", (60, 40), "white").save(d / "work" / "0.png")
    Image.new("RGB", (60, 40), "white").save(d / "input.png")
    (d / "result.json").write_text(json.dumps({
        "selected_examples": [{"instance_id": "1", "reactants": [{"E-SMILES": "CCO"}],
                               "products": [{"E-SMILES": "CCN"}], "conditions": [[]], "metrics": []}],
        "reaction_templates": []}), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({
        "id": job_id, "filename": "fig.png", "status": "done", "valid": True,
        "created": time.time()}), encoding="utf-8")
    return job_id


# ---------------------------------------------------------------- 6. figure jobs untouched
def test_figure_jobs_unchanged_and_export_shapes(world, monkeypatch):
    pdf_job = _register(world)
    monkeypatch.setattr(HS, "JOBS_DIR", world["jobs"])
    fig_job = _fake_figure_job(world["jobs"])

    # figure job: /api/jobs synthesizes n_examples from result.json; pdf endpoints 404
    rec = HS.api_job(fig_job)
    assert rec["n_examples"] == 1 and rec["status"] == "done"
    for fn, args in ((HS.api_pdf_meta, (fig_job,)), (HS.api_pdf_file, (fig_job,)),
                     (HS.api_pdf_page, (fig_job, 1)), (HS.api_pdf_anchor, (fig_job, 0)),
                     (HS.api_pdf_anchor_crop, (fig_job, 0))):
        with pytest.raises(HTTPException) as e:
            fn(*args)
        assert e.value.status_code == 404

    # figure export = GT-schema V2 (task_id, no anchors[]) — unchanged behavior
    fig_export = json.loads(HS.api_export(fig_job).body)
    assert "task_id" in fig_export and "anchors" not in fig_export

    # document export = the ORIGINAL paper-level prediction, byte-equivalent content
    pdf_export_response = HS.api_export(pdf_job)
    assert pdf_export_response.body == world["pred"].read_bytes()
    pdf_export = json.loads(pdf_export_response.body)
    assert pdf_export == json.loads(world["pred"].read_text())
    assert "task_id" not in pdf_export
    csv_text = HS.api_export(pdf_job, fmt="csv").body.decode()
    assert csv_text.splitlines()[0] == "anchor,page,kind,reaction,reactants,products,conditions,metrics"
    assert "Scheme 2" in csv_text and "CCO" in csv_text
    report = json.loads(HS.api_export(pdf_job, fmt="report").body)
    assert report["schema"] == "chemrxn.extraction-report.v1"
    assert report["source"]["kind"] == "paper"
    assert report["source"]["immutable"] is True and report["source"]["gt_included"] is False
    assert report["counts"] == {
        "reactions": 2,
        "examples": 2,
        "templates": 0,
        "human_adjudications": 0,
    }
    assert report["reactions"][0]["reactants"] == ["CCO"]
    rxn_response = HS.api_export(pdf_job, fmt="rxn")
    with zipfile.ZipFile(io.BytesIO(rxn_response.body)) as archive:
        names = archive.namelist()
        assert "manifest.json" in names
        rxn_manifest = json.loads(archive.read("manifest.json"))
        assert len(rxn_manifest["exported"]) == 2
        assert rxn_manifest["omitted"] == []
        assert all(name.endswith(".rxn") for name in names if name != "manifest.json")
        assert "$RXN" in archive.read(rxn_manifest["exported"][0]["file"]).decode()

    fig_report = json.loads(HS.api_export(fig_job, fmt="report").body)
    assert fig_report["source"]["kind"] == "figure"
    assert fig_report["counts"]["reactions"] == 1
    with pytest.raises(HTTPException) as exc:
        HS.api_export(fig_job, fmt="sdf")
    assert exc.value.status_code == 400

    sentinel_job = "sentinel-export"
    sentinel_dir = world["jobs"] / sentinel_job
    sentinel_dir.mkdir()
    (sentinel_dir / "result.json").write_text(
        json.dumps({"selected_examples": [], "note": "GT_SENTINEL"}),
        encoding="utf-8",
    )
    with pytest.raises(HTTPException) as exc:
        HS.api_export(sentinel_job, fmt="report")
    assert exc.value.status_code == 409

    for index, payload in enumerate((
        {"metadata": {"ground_truth": {"secret": "ordinary"}}},
        {"metadata": {"groundTruthData": {"secret": "ordinary"}}},
        {"metadata": {"gtIncluded": True}},
        {"metadata": {"reference": {"secret": "ordinary"}}},
    )):
        structural_gt_job = f"structural-gt-export-{index}"
        structural_gt_dir = world["jobs"] / structural_gt_job
        structural_gt_dir.mkdir()
        (structural_gt_dir / "result.json").write_text(
            json.dumps({"selected_examples": [], **payload}),
            encoding="utf-8",
        )
        with pytest.raises(HTTPException) as exc:
            HS.api_export(structural_gt_job, fmt="v2")
        assert exc.value.status_code == 409

    csv_job = _register(world, collection="csv-safety")
    csv_path = world["jobs"] / csv_job / "result.json"
    csv_pred = json.loads(csv_path.read_text())
    csv_pred["anchors"][0]["anchor_figure"] = "=WEBSERVICE(\"https://invalid.example\")"
    csv_pred["anchors"][0]["reaction_templates"] = [{
        "template_id": "+template",
        "reactants": [{"E-SMILES": "CC"}],
        "products": [{"E-SMILES": "CCC"}],
        "conditions": [[{"text": "@SUM(1,1)"}]],
        "metrics": [{"role": "yield", "text": "-2+3"}],
    }]
    csv_path.write_text(json.dumps(csv_pred), encoding="utf-8")
    rows = list(csv.reader(io.StringIO(HS.api_export(csv_job, fmt="csv").body.decode())))
    assert rows[0][0:4] == ["anchor", "page", "kind", "reaction"]
    assert len(rows) == 4
    template_row = next(row for row in rows if row[2] == "template")
    assert template_row[0].startswith("'=")
    assert template_row[3] == "'+template"
    assert template_row[6] == "'@SUM(1,1)"
    assert template_row[7] == "yield: -2+3"

    omission_job = _fake_figure_job(world["jobs"], "all-omitted-rxn")
    omission_path = world["jobs"] / omission_job / "result.json"
    omission_path.write_text(json.dumps({
        "reaction_templates": [{
            "template_id": "markush-only",
            "reactants": [{"E-SMILES": "CC"}],
            "products": [],
            "conditions": [],
            "metrics": [],
        }],
        "selected_examples": [],
    }), encoding="utf-8")
    omission_response = HS.api_export(omission_job, fmt="rxn")
    with zipfile.ZipFile(io.BytesIO(omission_response.body)) as archive:
        omission_manifest = json.loads(archive.read("manifest.json"))
        assert omission_manifest["exported"] == []
        assert len(omission_manifest["omitted"]) == 1
        assert omission_manifest["omitted"][0]["reason"] == "missing reactant or product SMILES"

    # document job card keeps its meta n_examples (total reactions), not len(selected_examples)=0
    rec_pdf = HS.api_job(pdf_job)
    assert rec_pdf["n_examples"] == 2


def test_pdf_source_rights_propagate_without_mutating_sealed_payloads(world, monkeypatch):
    job_id = _register(world, collection="attributed-export")
    job_dir = world["jobs"] / job_id
    monkeypatch.setattr(HS, "JOBS_DIR", world["jobs"])
    monkeypatch.setattr(HS, "_COLLECTIONS_DIR", world["jobs"] / "_collections")
    raw_before = (job_dir / "result.json").read_bytes()
    view_before = (job_dir / "work" / "pdf_view.json").read_bytes()
    rights = {
        "license": "cc-by-4.0",
        "attribution": "© 2026 Example et al.; licensed CC BY 4.0",
        "source_doi": "10.3762/example.1",
        "source_url": "https://doi.org/10.3762/example.1",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "change_notice": "Extraction and review annotations added; source PDF unchanged.",
    }
    validate_path = job_dir / "work" / "validate.json"
    validate = {}
    validate["counts"] = {
        **(validate.get("counts") or {}),
        "molecule_occurrences_located": 2,
        "molecule_occurrences_derived_unlocated": 2,
    }
    validate_path.write_text(json.dumps(validate), encoding="utf-8")
    _seal_test_source_rights(job_dir, rights)
    meta_path = job_dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["owner"] = "local-user"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(HS, "_identity", lambda _request: "local-user")

    public_job = HS.api_job(job_id)
    assert public_job["source_rights"] == rights
    assert public_job["source_rights_status"] == "verified"
    assert not any(key in public_job for key in rights)
    listed_job = next(
        item
        for item in HS.api_jobs(request=None, all=True)["jobs"]
        if item["id"] == job_id
    )
    assert listed_job["source_rights"] == rights
    assert listed_job["source_rights_status"] == "verified"
    assert listed_job["can_access"] is True
    assert listed_job["has_overlay"] is False
    assert "owner" not in listed_job
    assert not any(key in listed_job for key in rights)
    (job_dir / "work" / "0.png").write_bytes(b"source-figure")
    listed_figure_job = next(
        item
        for item in HS.api_jobs(request=None, all=True)["jobs"]
        if item["id"] == job_id
    )
    assert listed_figure_job["has_figure"] is True
    assert listed_figure_job["has_overlay"] is False
    (job_dir / "work" / "0_annotated.png").write_bytes(b"annotated-figure")
    listed_overlay_job = next(
        item
        for item in HS.api_jobs(request=None, all=True)["jobs"]
        if item["id"] == job_id
    )
    assert listed_overlay_job["has_overlay"] is True
    [collection_job] = HS.api_collection("attributed-export")["cards"]
    assert collection_job["source_rights"] == rights
    assert collection_job["source_rights_status"] == "verified"
    assert not any(key in collection_job for key in rights)
    pdf_meta = HS.api_pdf_meta(job_id)
    assert pdf_meta["job"]["source_rights"] == rights
    assert pdf_meta["job"]["source_rights_status"] == "verified"
    assert not any(key in pdf_meta["job"] for key in rights)
    assert not any(key in pdf_meta["validate"] for key in HS._SOURCE_RIGHTS_FIELDS)
    assert pdf_meta["view"]["stats"]["n_molecules_located"] == 2
    assert pdf_meta["view"]["stats"]["n_molecules_unlocated"] == 2
    assert pdf_meta["view"]["stats"]["n_molecules_derived_unlocated"] == 2

    report = json.loads(HS.api_export(job_id, fmt="report").body)
    assert report["source"]["rights"] == rights
    rights_sidecar = json.loads(HS.api_export(job_id, fmt="rights").body)
    assert rights_sidecar["schema"] == "chemrxn.source-rights.v1"
    assert rights_sidecar["source_rights"] == rights
    assert rights_sidecar["source_prediction_sha256"] == hashlib.sha256(raw_before).hexdigest()
    with zipfile.ZipFile(io.BytesIO(HS.api_export(job_id, fmt="rxn").body)) as archive:
        assert json.loads(archive.read("manifest.json"))["source_rights"] == rights

    review_dir = job_dir / "review"
    review_dir.mkdir()
    (review_dir / "adjudications.json").write_text(json.dumps({
        "schema": "pdf_adjudications_v1",
        "items": [{
            "difference_id": "rights-row",
            "verdict": "prediction accepted",
            "note": "human reviewed",
            "correction": None,
            "updated_at": 1.0,
        }],
    }), encoding="utf-8")
    assert json.loads(HS.api_review_export(job_id).body)["source"]["rights"] == rights
    assert json.loads(HS.api_pdf_review_export(job_id).body)["source_rights"] == rights

    assert HS.api_export(job_id, fmt="v2").body == raw_before
    assert (job_dir / "result.json").read_bytes() == raw_before
    assert (job_dir / "work" / "pdf_view.json").read_bytes() == view_before
    assert GT_SENTINEL not in json.dumps({
        "meta": pdf_meta,
        "report": report,
        "rights": rights_sidecar,
    })

    # Flat meta is only a cache hint. Mutating it cannot forge or suppress the
    # independently verified proof projection.
    incomplete_meta = {**meta, "license": "cc-by-4.0", "attribution": ""}
    meta_path.write_text(json.dumps(incomplete_meta), encoding="utf-8")
    HS._SOURCE_RIGHTS_CACHE.clear()
    assert json.loads(HS.api_export(job_id, fmt="rights").body)["source_rights"] == rights

    # Any proof-chain mutation fails closed for attribution-bearing exports.
    proof_path = job_dir / "work" / "source_rights.json"
    proof = json.loads(proof_path.read_text())
    proof["rights"]["attribution"] = "forged attribution"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    HS._SOURCE_RIGHTS_CACHE.clear()
    with pytest.raises(HTTPException) as exc:
        HS.api_export(job_id, fmt="rights")
    assert exc.value.status_code == 409
    invalid_meta = HS.api_pdf_meta(job_id)
    assert invalid_meta["job"]["source_rights"] is None
    assert invalid_meta["job"]["source_rights_status"] == "invalid"
    assert not any(
        key in json.dumps(invalid_meta["validate"])
        for key in HS._SOURCE_RIGHTS_FIELDS
    )


@pytest.mark.parametrize(
    "relative",
    [
        "result.json",
        "work/pdf_view.json",
        "work/validate.json",
        "work/handoff_manifest.json",
        "work/handoff_SHA256SUMS.txt",
        "work/main.pdf",
    ],
)
def test_source_rights_proof_fails_closed_on_bound_artifact_tamper(
    world,
    monkeypatch,
    relative,
):
    job_id = _register(world, collection=f"proof-tamper-{Path(relative).name}")
    job_dir = world["jobs"] / job_id
    monkeypatch.setattr(HS, "JOBS_DIR", world["jobs"])
    rights = {
        "license": "cc-by-4.0",
        "attribution": "Example Authors",
        "source_url": "https://example.org/article",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "change_notice": "Structured extraction metadata added; source PDF unchanged.",
    }
    _seal_test_source_rights(job_dir, rights)
    target = job_dir / relative
    target.write_bytes(target.read_bytes() + b" ")
    HS._SOURCE_RIGHTS_CACHE.clear()

    pdf_meta = HS.api_pdf_meta(job_id)
    assert pdf_meta["job"]["source_rights"] is None
    assert pdf_meta["job"]["source_rights_status"] == "invalid"
    with pytest.raises(HTTPException) as exc:
        HS.api_export(job_id, fmt="rights")
    assert exc.value.status_code == 409


@pytest.mark.parametrize(
    ("relative", "proof_key", "validation_key"),
    [
        ("result.json", "result_json", "result.json"),
        ("work/pdf_view.json", "pdf_view_json", "pdf_view.json"),
    ],
)
def test_source_rights_proof_rejects_coherent_reseal_not_in_original_inventory(
    world,
    monkeypatch,
    relative,
    proof_key,
    validation_key,
):
    """A mutable local proof cannot re-root a sealed handoff payload.

    This simulates a coherent rewrite of the installed payload, validation
    record, and proof while deliberately leaving the upstream SHA256SUMS
    inventory unchanged.
    """
    job_id = _register(world, collection=f"proof-reseal-{proof_key}")
    job_dir = world["jobs"] / job_id
    monkeypatch.setattr(HS, "JOBS_DIR", world["jobs"])
    rights = {
        "license": "cc-by-4.0",
        "attribution": "Example Authors",
        "source_url": "https://example.org/article",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "change_notice": "Structured extraction metadata added; source PDF unchanged.",
    }
    _seal_test_source_rights(job_dir, rights)

    target = job_dir / relative
    target.write_bytes(target.read_bytes() + b" ")
    replacement_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    validation_path = job_dir / "work" / "validate.json"
    validation = json.loads(validation_path.read_text())
    validation["hashes"][validation_key] = replacement_sha
    validation_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    proof_path = job_dir / "work" / "source_rights.json"
    proof = json.loads(proof_path.read_text())
    proof["hashes"][proof_key] = replacement_sha
    proof["hashes"]["validation_json"] = hashlib.sha256(
        validation_path.read_bytes(),
    ).hexdigest()
    proof_path.write_text(
        json.dumps(proof, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    HS._SOURCE_RIGHTS_CACHE.clear()

    pdf_meta = HS.api_pdf_meta(job_id)
    assert pdf_meta["job"]["source_rights"] is None
    assert pdf_meta["job"]["source_rights_status"] == "invalid"
    assert not any(
        key in json.dumps(pdf_meta["validate"])
        for key in HS._SOURCE_RIGHTS_FIELDS
    )
    with pytest.raises(HTTPException) as exc:
        HS.api_export(job_id, fmt="rights")
    assert exc.value.status_code == 409


def test_review_export_is_explicit_gt_free_and_keeps_raw_bytes_immutable(world, monkeypatch):
    monkeypatch.setattr(HS, "JOBS_DIR", world["jobs"])
    reviewer_root = world["tmp"] / "reviewer"
    monkeypatch.setattr(HS, "PDF_ROOT", reviewer_root)

    # Figure review exports a complete reviewed V2 snapshot while preserving
    # the runtime result bytes as the immutable raw source.
    fig_job = _fake_figure_job(world["jobs"], "reviewed-figure")
    fig_dir = world["jobs"] / fig_job
    fig_meta_path = fig_dir / "meta.json"
    fig_meta = json.loads(fig_meta_path.read_text())
    fig_meta["reviewer_url"] = "http://127.0.0.1:8767/#/upload_reviewed-figure/0"
    fig_meta_path.write_text(json.dumps(fig_meta), encoding="utf-8")
    figure_review_dir = reviewer_root / "upload_reviewed-figure"
    figure_review_dir.mkdir(parents=True)
    reviewer_source = {
        "selected_examples": [{
            "instance_id": "1",
            "reactants": [{"E-SMILES": "CCO"}],
            "products": [{"E-SMILES": "CCN"}],
            "conditions": [[]],
            "metrics": [],
        }],
        "reaction_templates": [],
    }
    reviewer_source_bytes = json.dumps(reviewer_source).encode()
    (figure_review_dir / "0_predicted_output.json").write_bytes(reviewer_source_bytes)
    reviewed_figure = {
        "selected_examples": [{
            "instance_id": "1",
            "reactants": [{"E-SMILES": "CCO"}],
            "products": [{"E-SMILES": "CCCl"}],
            "conditions": [[]],
            "metrics": [],
        }],
        "reaction_templates": [],
        "_reviewed_meta": {
            "version": 2,
            "proposal_id": "proposal-sha",
            "reviewed_at": "2026-07-27T00:00:00Z",
            "source_sha256": hashlib.sha256(reviewer_source_bytes).hexdigest(),
        },
    }
    (figure_review_dir / "0_predicted_output.reviewed.json").write_text(
        json.dumps(reviewed_figure), encoding="utf-8",
    )
    figure_raw_before = (fig_dir / "result.json").read_bytes()
    figure_response = HS.api_review_export(fig_job)
    figure_payload = json.loads(figure_response.body)
    assert figure_payload["schema"] == "chemrxn.review-export.v1"
    assert figure_payload["target"] == "figure"
    assert figure_payload["head_version"] == 2
    assert figure_payload["reviewed_prediction"]["selected_examples"][0]["products"][0]["E-SMILES"] == "CCCl"
    assert figure_payload["materialization"] == "complete_reviewed_figure_prediction"
    assert figure_payload["source"]["review_lineage_verified"] is True
    assert figure_payload["source"]["evaluation_artifacts_included"] is False
    assert (fig_dir / "result.json").read_bytes() == figure_raw_before
    (figure_review_dir / "0_predicted_output.json").write_text(
        json.dumps({**reviewer_source, "note": "different source"}),
        encoding="utf-8",
    )
    with pytest.raises(HTTPException) as exc:
        HS.api_review_export(fig_job)
    assert exc.value.status_code == 409
    (figure_review_dir / "0_predicted_output.json").write_bytes(reviewer_source_bytes)

    # PDF review exports the adjudication/version sidecar and explicitly does
    # not claim that a corrected whole-document prediction was materialized.
    pdf_job = _register(world, collection="review-export")
    pdf_dir = world["jobs"] / pdf_job
    monkeypatch.setattr(
        HS,
        "LOCAL_RUNTIME_USER_ID",
        json.loads((pdf_dir / "meta.json").read_text())["owner"],
    )
    proposal = HS._pdf_review_proposal(pdf_dir, {
        "base_version": 0,
        "after": {
            "difference_id": "pred:0:0",
            "verdict": "correct fields",
            "note": "source text confirms the temperature",
            "correction": {"condition_text": "80 °C"},
            "updated_at": 1.0,
        },
        "proposed_at": "2026-07-27T00:00:00Z",
    })

    class ApplyPdfReview:
        cookies = {}

        async def json(self):
            return {"proposal": proposal}

    asyncio.run(HS.api_pdf_review_apply(pdf_job, ApplyPdfReview()))
    pdf_raw_before = (pdf_dir / "result.json").read_bytes()
    pdf_payload = json.loads(HS.api_review_export(pdf_job).body)
    assert pdf_payload["target"] == "pdf"
    assert pdf_payload["head_version"] == 1
    assert pdf_payload["counts"] == {"correct fields": 1}
    assert pdf_payload["geometry_count"] == 0
    assert pdf_payload["geometry_corrections"] == []
    assert pdf_payload["reviewed_prediction"] is None
    assert pdf_payload["materialization"] == "review_sidecar_only"
    assert "compare_summary" not in pdf_payload
    assert GT_SENTINEL not in json.dumps(pdf_payload)
    assert (pdf_dir / "result.json").read_bytes() == pdf_raw_before

    legacy_job = _register(world, collection="legacy-review-export")
    legacy_review_dir = world["jobs"] / legacy_job / "review"
    legacy_review_dir.mkdir()
    (legacy_review_dir / "adjudications.json").write_text(json.dumps({
        "schema": "pdf_adjudications_v1",
        "items": [{
            "difference_id": "legacy-row",
            "verdict": "prediction accepted",
            "note": "legacy benchmark adjudication",
            "correction": None,
            "updated_at": 1.0,
        }],
    }), encoding="utf-8")
    legacy_payload = json.loads(HS.api_review_export(legacy_job).body)
    assert legacy_payload["head_version"] == 0
    assert legacy_payload["counts"] == {"prediction accepted": 1}
    assert legacy_payload["adjudications"][0]["difference_id"] == "legacy-row"
    legacy_summary = json.loads(HS.api_pdf_review_export(legacy_job).body)
    assert legacy_summary["schema"] == "pdf_review_summary_v1"
    assert legacy_summary["counts"] == {"prediction accepted": 1}
    assert "compare_summary" not in legacy_summary

    no_review_job = _fake_figure_job(world["jobs"], "no-review")
    no_review_meta = json.loads((world["jobs"] / no_review_job / "meta.json").read_text())
    no_review_meta["reviewer_url"] = "http://127.0.0.1:8767/#/upload_no-review/0"
    (world["jobs"] / no_review_job / "meta.json").write_text(
        json.dumps(no_review_meta), encoding="utf-8",
    )
    with pytest.raises(HTTPException) as exc:
        HS.api_review_export(no_review_job)
    assert exc.value.status_code == 409

    empty_pdf_job = _register(world, collection="empty-review-summary")
    empty_summary = json.loads(HS.api_pdf_review_export(empty_pdf_job).body)
    assert empty_summary["counts"] == {}
    assert empty_summary["adjudications"] == []

    poisoned_job = _fake_figure_job(world["jobs"], "poisoned-review-url")
    poisoned_meta_path = world["jobs"] / poisoned_job / "meta.json"
    poisoned_meta = json.loads(poisoned_meta_path.read_text())
    poisoned_meta["reviewer_url"] = (
        "http://127.0.0.1:8767/go?next=/#/upload_reviewed-figure/0#not-a-task"
    )
    poisoned_meta_path.write_text(json.dumps(poisoned_meta), encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        HS.api_review_export(poisoned_job)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------- 7. GT absent by default
def test_gt_absent_by_default_and_sentinel_never_leaks(world):
    job_id = _register(world)
    jd = world["jobs"] / job_id
    # no reviewed/gt files anywhere in the job dir, and the GT sentinel string never leaks
    assert not list(jd.rglob("*reviewed*")) and not list(jd.rglob("gt.json"))
    for f in jd.rglob("*.json"):
        assert GT_SENTINEL not in f.read_text(encoding="utf-8"), f
    # adapter also works when NO GT path exists at all
    shutil.rmtree(world["pool"] / "50__paper" / "2")
    (world["jobs"] / "_collections" / "docviz.jsonl").unlink()
    shutil.rmtree(jd)
    assert _register(world) == job_id


def test_reviewer_mirror_optin_and_gt_flag_posthoc_only(world):
    reviewer = world["tmp"] / "reviewer_pdf"
    reviewer.mkdir()
    job_id = _register(world, collection="docviz2", reviewer_pdf_root=reviewer, with_gt=False)
    slugs = sorted(p.name for p in reviewer.iterdir())
    assert slugs and all(s.startswith("upload_db_docviz2_p50__a") for s in slugs)
    for s in slugs:
        assert (reviewer / s / "0_predicted_output.json").exists()
        assert not list((reviewer / s).glob("*reviewed*"))     # GT only via explicit --with-gt
    view = _view(world, job_id)
    assert view["anchors"][0]["reviewer_slug"] in slugs
    # and the job store itself still carries no GT either way
    assert not list((world["jobs"] / job_id).rglob("*reviewed*"))


# ---------------------------------------------------------------- 8. JS syntax + API smoke
def test_frontend_js_syntax(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed on this host — JS syntax check skipped (report this)")
    html = (REPO / "service" / "harness_index.html").read_text(encoding="utf-8")
    start = html.rindex("<script>") + len("<script>")
    end = html.rindex("</script>")
    js = tmp_path / "ui.js"
    js.write_text(html[start:end], encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]


def test_index_serves_document_mode_markup():
    resp = HS.index()
    body = resp.body.decode("utf-8")
    for marker in ('id="docmode"', 'id="docbboxpanel"', 'id="docright"',
                   "pdf-bbox-render", "openDoc"):
        assert marker in body
    h = HS.healthz()
    assert h["ok"] is True and h["ui"]["document_mode"] is True


def test_document_overlay_renders_unjoined_raw_molparser_boxes_without_duplicates():
    body = HS.index().body.decode("utf-8")
    assert "DOC.view.raw_molparser_detections||[]" in body
    assert "coveredMoleculeBoxes.has(rawKey)||overlapsPrediction" in body
    assert "intersection/union>=0.8" in body
    assert 'kind:"raw-molparser"' in body
    assert "selectable:false" in body
    assert 'stroke:"#2563eb"' in body
    assert 'rawmolparser:"true"' in body


def test_document_navigation_keeps_page_grounded_prose_anchors_visible():
    """A reaction-bearing SI preparation remains navigable without fake crop geometry."""
    body = HS.index().body.decode("utf-8")
    assert "docItemSource(a)===DOC.source&&a.page&&a.n_reactions>0" in body
    assert "function docCropAnchorList(){return docAnchorList().filter(a=>a.bbox);}" in body
    assert 'if(!a?.bbox) return {x:0,y:0,width:pg.width,height:pg.height,page:pg};' in body
    assert 'DOC.frame=DOC.productMode||!anchor.bbox?"page":"anchor";' in body
    assert "!docAnchor()?.bbox||docGtNeedsPage()" in body
    assert 'if(DOC.frame==="anchor"&&!docAnchor()?.bbox)DOC.frame="page";' in body
    assert 'DOC._imageUrl=(DOC.frame==="anchor"&&anchor?.bbox)' in body


def test_health_fails_when_ui_bundle_disappears(tmp_path, monkeypatch):
    """A Python process may outlive a deleted worktree; endpoints alive + fallback HTML is NOT healthy."""
    monkeypatch.setattr(HS, "REPO", tmp_path)
    body = HS.index().body.decode("utf-8")
    assert 'id="docmode"' not in body
    h = HS.healthz()
    assert h["ok"] is False and h["ui"]["document_mode"] is False


# ------------------------------------------------ #119: main/SI anchor namespace
def _mixed_pred_and_view():
    """A prediction whose anchors run main (0..m-1) then SI, paired with a pdf_view whose
    anchor index is PER-DOCUMENT local (main 0..m-1, si 0..s-1) while its molecules already
    use the GLOBAL index (si == m+j). This is the exact shape that made SI reactions jump
    back to the main text in issue #119."""
    prediction = {"anchors": [
        {"_source": {"source_document": "main"}},   # global 0
        {"_source": {"source_document": "main"}},   # global 1
        {"_source": {"source_document": "si"}},     # global 2
        {"_source": {"source_document": "si"}},      # global 3
    ]}
    view = {
        "anchors": [
            {"index": 0, "source_document": "main", "page": 2},
            {"index": 1, "source_document": "main", "page": 4},
            {"index": 0, "source_document": "si", "page": 16},   # local 0 -> global 2
            {"index": 1, "source_document": "si", "page": 25},   # local 1 -> global 3
        ],
        # molecules are already global (the other half of the inconsistency)
        "molecules": [{"anchor": 2, "source_document": "si"}, {"anchor": 3, "source_document": "si"}],
    }
    return prediction, view


def test_reconcile_pdf_view_anchor_index_namespaces_si():
    prediction, view = _mixed_pred_and_view()
    out = HS._reconcile_pdf_view_anchor_index(view, prediction)
    by_source = {}
    for a in out["anchors"]:
        by_source.setdefault(a["source_document"], []).append((a["index"], a.get("index_local")))
    # main anchors keep their (already-global) index; SI anchors are lifted into the global space
    assert by_source["main"] == [(0, None), (1, None)]
    assert by_source["si"] == [(2, 0), (3, 1)]
    # SI anchor index now agrees with the (already-global) molecule anchors -> one index space
    mol_si = sorted(m["anchor"] for m in out["molecules"] if m["source_document"] == "si")
    anc_si = sorted(a["index"] for a in out["anchors"] if a["source_document"] == "si")
    assert mol_si == anc_si == [2, 3]


def test_reconcile_pdf_view_anchor_index_is_idempotent():
    prediction, view = _mixed_pred_and_view()
    once = HS._reconcile_pdf_view_anchor_index(view, prediction)
    twice = HS._reconcile_pdf_view_anchor_index(once, prediction)
    assert [a["index"] for a in once["anchors"]] == [a["index"] for a in twice["anchors"]]


def test_reconcile_pdf_view_anchor_index_noop_without_prediction():
    _, view = _mixed_pred_and_view()
    # no prediction anchors -> return the view unchanged (never guess a namespace)
    assert HS._reconcile_pdf_view_anchor_index(view, {"anchors": None}) is view
    assert HS._reconcile_pdf_view_anchor_index(view, None) is view


def test_reconcile_leaves_already_global_reordered_view_untouched():
    """codex #119 review: an already-global view whose anchors are REORDERED must not be relabelled
    by array position (that would swap geometry)."""
    prediction = {"anchors": [
        {"_source": {"source_document": "main"}},   # global 0
        {"_source": {"source_document": "main"}},   # global 1
        {"_source": {"source_document": "si"}},     # global 2
        {"_source": {"source_document": "si"}},      # global 3
    ]}
    view = {"anchors": [
        {"index": 3, "source_document": "si", "page": 25},   # already global, reversed
        {"index": 2, "source_document": "si", "page": 16},
    ]}
    out = HS._reconcile_pdf_view_anchor_index(view, prediction)
    assert out is view  # unchanged object -> no relabelling
    assert [a["index"] for a in out["anchors"]] == [3, 2]


def test_reconcile_local_view_maps_by_own_index_not_array_order():
    """A local SI view given out of array order still maps each anchor by its OWN local index."""
    prediction = {"anchors": [
        {"_source": {"source_document": "main"}},   # global 0
        {"_source": {"source_document": "main"}},   # global 1
        {"_source": {"source_document": "si"}},     # global 2 (local 0)
        {"_source": {"source_document": "si"}},      # global 3 (local 1)
    ]}
    view = {"anchors": [
        {"index": 1, "source_document": "si", "page": 25},   # local 1 -> global 3
        {"index": 0, "source_document": "si", "page": 16},   # local 0 -> global 2
    ]}
    out = HS._reconcile_pdf_view_anchor_index(view, prediction)
    assert [a["index"] for a in out["anchors"]] == [3, 2]   # each by its own local index
    assert [a["page"] for a in out["anchors"]] == [25, 16]  # geometry preserved


def test_reconcile_si_only_document_is_global_from_zero():
    """An SI-only prediction: SI anchors ARE global starting at 0 -> a 0-based view is left alone."""
    prediction = {"anchors": [
        {"_source": {"source_document": "si"}},
        {"_source": {"source_document": "si"}},
    ]}
    view = {"anchors": [
        {"index": 0, "source_document": "si"},
        {"index": 1, "source_document": "si"},
    ]}
    out = HS._reconcile_pdf_view_anchor_index(view, prediction)
    assert [a["index"] for a in out["anchors"]] == [0, 1]
