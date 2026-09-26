"""Local-first PDF result reviewer — issue #7 test list (importer / GT compare / endpoints / launcher).

Anti-leakage contract under test: the DEFAULT import never opens GT; --with-gt is explicit and
post-hoc; GT lives only under <job>/gt/ (+ reviewer sidecars) and NEVER enters result.json,
pdf_view.json or any extraction input. Comparison uses the FROZEN scorer as a read-only adapter.
"""
import asyncio
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
for p in (SCRIPTS, REPO.parent / "pdf", REPO.parent / "pdf" / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

fitz = pytest.importorskip("fitz", reason="local reviewer needs pymupdf")
pytest.importorskip("rdkit", reason="the frozen scorer needs rdkit")
pytest.importorskip("scipy", reason="the frozen scorer needs scipy")

import import_pdf_result as IMP  # noqa: E402
import harness_server as HS      # noqa: E402
import preds_to_viewer as PV      # noqa: E402
from fastapi import HTTPException  # noqa: E402

GT_SENTINEL = "GT_SENTINEL_MUST_STAY_IN_SIDECARS_0xBEEF"


def _make_pdf(path: Path, n_pages=3, w=612, h=792):
    doc = fitz.open()
    for i in range(n_pages):
        doc.new_page(width=w, height=h).insert_text((72, 72), f"synthetic page {i + 1}")
    doc.save(str(path))
    doc.close()


def _pred():
    return {
        "schema_version": "rxnharbor-pred-v2", "coordinate_space": "pdf_points_v1",
        "paper_id": "50",
        "anchors": [
            {"anchor_kind": "scheme", "anchor_figure": "Scheme 2", "anchor_page": 2,
             "anchor_bbox_pdf": [48.0, 443.0, 554.0, 519.0], "identifier_map": {},
             "selected_examples": [
                 {"instance_id": "1",
                  "reactants": [{"identifier": "10", "E-SMILES": "CCO",
                                 "_source": {"origin": "image", "type": "image_structure",
                                             "bbox": [100.0, 100.0, 160.0, 140.0],
                                             "bbox_page": 2, "bbox_frame": "pdf_points"}}],
                  "products": [{"identifier": "11", "E-SMILES": "CCN",
                                "_source": {"origin": "image", "type": "image_structure"}}],
                  "conditions": [[{"role": "reagent", "text": "NH3"}]], "metrics": []}],
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


def _gt_region(figure, page, bbox, examples):
    return {"task_id": f"t_{figure}", "paper_id": "50", "anchor_figure": figure,
            "anchor_page": page, "anchor_bbox_pdf": bbox, "identifier_map": {},
            "selected_examples": examples, "reaction_templates": []}


def _gt_examples():
    """region 1 (Scheme 1): unmatched by the pred -> anchor 'missed'; region 2 (Scheme 2): one
    exact match (with a DIFFERENT product identifier) + one GT-only -> anchor 'partial'."""
    r1 = [{"instance_id": "g1",
           "reactants": [{"identifier": "1", "E-SMILES": "c1ccccc1",
                          "_source": {"type": "image_structure", "bbox": [1034, 109, 1268, 279],
                                      "bbox_index": 0}}],
           "products": [{"identifier": "2", "E-SMILES": "c1ccncc1",
                         "description": GT_SENTINEL,
                         "_source": {"type": "image_structure", "bbox": [30, 40, 400, 200],
                                     "bbox_index": 1}}],
           "conditions": [[{"role": "reagent", "text": "secret reagent"}]], "metrics": []}]
    r2 = [{"instance_id": "1",
           "reactants": [{"identifier": "10", "E-SMILES": "CCO",
                          "_source": {"type": "image_structure", "bbox": [900, 80, 1100, 200]}}],
           "products": [{"identifier": "11-gt", "E-SMILES": "CCN",
                         "_source": {"type": "image_structure", "bbox": [1200, 80, 1500, 200]}}],
           "conditions": [[{"role": "reagent", "text": "NH3"}]], "metrics": []},
          {"instance_id": "1b",
           "reactants": [{"identifier": "14", "E-SMILES": "CC(C)O", "_source": {"type": "image_structure"}}],
           "products": [{"identifier": "15", "E-SMILES": "CC(C)N", "_source": {"type": "image_structure"}}],
           "conditions": [[]], "metrics": []}]
    return r1, r2


@pytest.fixture()
def world(tmp_path):
    pdf = tmp_path / "50_main.pdf"
    _make_pdf(pdf)
    pred_path = tmp_path / "pred.json"
    pred_path.write_text(json.dumps(_pred(), indent=1), encoding="utf-8")
    score_path = tmp_path / "score.jsonl"
    score_path.write_text(json.dumps({"paper": "50", "reward": 0.5, "overall": 0.4,
                                      "B_grounding": 0.3, "C_conditions": 0.2,
                                      "D_provenance": 0.1, "E_struct_dag": 0.6}) + "\n")
    r1, r2 = _gt_examples()
    gt_rel = tmp_path / "gt_released" / "50__paper"
    (gt_rel / "1").mkdir(parents=True)
    (gt_rel / "2").mkdir(parents=True)
    (gt_rel / "1" / "gt.json").write_text(json.dumps(
        _gt_region("Scheme 1", 2, [50.4, 225.2, 500.2, 438.0], r1)), encoding="utf-8")
    (gt_rel / "2" / "gt.json").write_text(json.dumps(
        _gt_region("Scheme 2", 2, [48.0, 443.9, 553.9, 519.2], r2)), encoding="utf-8")
    gt_rev = tmp_path / "gt_reviewed" / "50__paper"
    gt_rev.mkdir(parents=True)
    (gt_rev / "1_predicted_output.reviewed.json").write_text(json.dumps(
        _gt_region("Scheme 1", 2, [50.4, 225.2, 500.2, 438.0], r1)), encoding="utf-8")
    (gt_rev / "2_output.reviewed.json").write_text(json.dumps(
        _gt_region("Scheme 2", 2, [48.0, 443.9, 553.9, 519.2], r2)), encoding="utf-8")
    normalized = {"paper_id": "50", "schema_version": "rxnharbor-gt-v2",
                  "coordinate_space": "pdf_points_v1", "anchors": []}
    for idx, g in enumerate((json.loads((gt_rel / "1" / "gt.json").read_text()),
                             json.loads((gt_rel / "2" / "gt.json").read_text()))):
        g["anchor_index"] = idx
        for e in g["selected_examples"]:
            for side in ("reactants", "products"):
                for mi, m in enumerate(e.get(side, [])):
                    src = m.get("_source") or {}
                    if src.get("bbox"):
                        src["bbox_crop_px"] = src["bbox"]
                        src["bbox"] = [100.0 + 30 * mi, 250.0, 125.0 + 30 * mi, 275.0]
                        src["bbox_page"] = 2
                        src["bbox_frame"] = "pdf_points"
        normalized["anchors"].append(g)
    normalized_path = tmp_path / "50_gt_v2.json"
    normalized_path.write_text(json.dumps(normalized), encoding="utf-8")

    release_paper = tmp_path / "release" / "rel1" / "50__paper"
    release_paper.mkdir(parents=True)
    (release_paper / "50__paper.json").write_text(json.dumps({
        "screenshots": [
            {"id": 1, "page": 2, "bbox_pdf": [50.4, 225.2, 500.2, 438.0],
             "image_size": [1500, 500]},
            {"id": 2, "page": 2, "bbox_pdf": [48.0, 443.9, 553.9, 519.2],
             "image_size": [1500, 500]},
        ]}), encoding="utf-8")
    data = tmp_path / "data"
    return {"tmp": tmp_path, "pdf": pdf, "pred": pred_path, "score": score_path,
            "gt_released": tmp_path / "gt_released", "gt_reviewed": tmp_path / "gt_reviewed",
            "gt_paper": normalized_path, "release": tmp_path / "release", "data": data}


def _import(world, collection="r0", extra=None):
    argv = ["--pdf", str(world["pdf"]), "--pred", str(world["pred"]),
            "--score", str(world["score"]), "--data-root", str(world["data"]),
            "--collection", collection, "--no-reviewer"] + (extra or [])
    return IMP.main(argv)


# ---------------------------------------------------------------- default = GT-free
def test_default_import_never_reads_gt(world):
    job_id = _import(world)
    jd = world["data"] / "jobs" / job_id
    assert jd.is_dir() and not (jd / "gt").exists()
    meta = json.loads((jd / "meta.json").read_text())
    assert meta["owner"] == IMP.LOCAL_IMPORT_OWNER == "local-user"
    for f in (world["data"] / "jobs").rglob("*"):
        if f.is_file():
            assert GT_SENTINEL not in f.read_text(encoding="utf-8", errors="ignore"), f
    # staging fully cleaned; exactly one card
    assert not any((world["data"] / "jobs" / ".staging").iterdir())
    rows = (world["data"] / "jobs" / "_collections" / "r0.jsonl").read_text().splitlines()
    assert len(rows) == 1
    # validator report stored for the supervision display
    v = json.loads((jd / "work" / "validate.json").read_text())
    assert v["n_pages"] == 3 and "by_anchor" in v


def test_gt_root_requires_explicit_flag(world):
    with pytest.raises(SystemExit):
        _import(world, extra=["--gt-root", str(world["gt_released"])])


def test_fatal_validation_leaves_no_partial_job(world):
    bad = json.loads(world["pred"].read_text())
    bad["schema_version"] = "nonsense-v9"
    world["pred"].write_text(json.dumps(bad))
    with pytest.raises(SystemExit):
        _import(world)
    jobs = world["data"] / "jobs"
    assert not (jobs / "_collections" / "r0.jsonl").exists()
    assert not [d for d in jobs.iterdir() if d.is_dir() and d.name != ".staging"] \
        if jobs.exists() else True
    if (jobs / ".staging").exists():
        assert not any((jobs / ".staging").iterdir())


def test_reimport_is_idempotent_upsert(world):
    j1 = _import(world)
    j2 = _import(world, extra=["--title", "round zero (repaired)"])
    assert j1 == j2
    jobs = world["data"] / "jobs"
    assert [d.name for d in jobs.iterdir() if d.is_dir() and d.name != "_collections"
            and d.name != ".staging"] == [j1]
    rows = [json.loads(x) for x in (jobs / "_collections" / "r0.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["filename"] == "round zero (repaired)"


# ---------------------------------------------------------------- explicit post-hoc GT
def test_with_gt_released_layout_compare(world):
    # reference import WITHOUT GT first: its view index must be byte-identical after GT attach
    ref = _import(world, collection="ref")
    ref_view = (world["data"] / "jobs" / ref / "work" / "pdf_view.json").read_bytes()

    job_id = _import(world, extra=["--with-gt", "--gt-root", str(world["gt_released"])])
    jd = world["data"] / "jobs" / job_id
    for f in ("gt_paper.json", "compare.json", "import.json"):
        assert (jd / "gt" / f).exists()
    # the pred and the view index never absorb GT
    assert (jd / "result.json").read_bytes() == world["pred"].read_bytes()
    assert (jd / "work" / "pdf_view.json").read_bytes() == ref_view
    for name in ("result.json", "meta.json"):
        assert GT_SENTINEL not in (jd / name).read_text()
    assert GT_SENTINEL not in (jd / "work" / "pdf_view.json").read_text()
    assert GT_SENTINEL in (jd / "gt" / "gt_paper.json").read_text()   # sidecar only

    cmp_d = json.loads((jd / "gt" / "compare.json").read_text())
    s = cmp_d["summary"]
    assert s["matched"] == 1 and s["gt_only"] == 2 and s["pred_only"] == 1
    assert s["n_gt"] == 3 and s["n_pred"] == 2
    assert isinstance(s["axes"]["A"], float) and s["loop_score"]["reward"] == 0.5
    # GT molecule boxes are crop pixels and are stripped: never publish anchor-only B or a
    # renormalized overall as though they were comparable to the official loop score.
    assert s["axes"]["B"] is None and s["overall"] is None
    assert s["B_anchor_only"] == pytest.approx(2 / 3)
    by_label = {g["label"]: g for g in cmp_d["gt_anchors"]}
    assert by_label["Scheme 1"]["status"] == "missed"
    assert by_label["Scheme 2"]["status"] == "partial"
    assert by_label["Scheme 1"]["bbox"] == [50.4, 225.2, 500.2, 438.0]  # page points, drawable
    # matched row: identifier mismatch on the product is visible, chemistry matched
    matched = [r for r in cmp_d["reactions"] if r["status"] == "matched"]
    assert len(matched) == 1
    pm = matched[0]["diff"]["products"]["matched"]
    assert pm and pm[0]["identifier_match"] is False
    # GT molecule boxes were crop pixels -> stripped to bbox_crop_px, never page boxes
    gtp = json.loads((jd / "gt" / "gt_paper.json").read_text())
    for a in gtp["anchors"]:
        for r in a["selected_examples"]:
            for m in r["reactants"] + r["products"]:
                assert "bbox" not in (m.get("_source") or {})
    # post-hoc marker log
    imp = json.loads((jd / "gt" / "import.json").read_text())
    assert imp["mode"] == "post-hoc-explicit" and len(imp["regions"]) == 2


def test_explicit_normalized_gt_draws_molecules_and_restores_full_b(world):
    # Release anchor geometry alone is intentionally insufficient (historical crop padding is not
    # encoded there). Full molecule overlays require an explicit normalized paper GT.
    job_id = _import(world, collection="geo_gt", extra=[
        "--with-gt", "--gt-root", str(world["gt_released"]),
        "--gt-paper", str(world["gt_paper"]),
    ])
    jd = world["data"] / "jobs" / job_id
    gt = json.loads((jd / "gt" / "gt_paper.json").read_text())
    geom = gt["_gt_geometry"]
    assert geom == {"n_bbox_pdf": 4, "n_bbox_crop_only": 0, "complete": True,
                    "source": "explicit_normalized_gt"}
    cmp_d = json.loads((jd / "gt" / "compare.json").read_text())
    s = cmp_d["summary"]
    assert isinstance(s["axes"]["B"], float) and isinstance(s["overall"], float)
    assert s["B_anchor_only"] is None and len(cmp_d["gt_molecules"]) == 4
    assert s["provenance"] and s["diagnoses"]


def test_reviewer_v2_raw_molparser_join_and_gt_byte_isolation(world):
    perception = world["tmp"] / "perception"
    perception.mkdir()
    (perception / "layout.json").write_text(json.dumps({"regions": [
        {"region_id": "2", "label": "Scheme 2", "page": 2,
         "bbox_pdf_norm": [0.1, 0.1, 0.9, 0.8]},
    ]}))
    (perception / "2_molparser.json").write_text(json.dumps([
        {"bbox_index": 8, "md5": "raw-md5-8", "smi": "CCO", "caption": "compound 10",
         "esmi": "CCO<sep>10", "bbox": [10, 10, 80, 60], "score": 0.91},
        {"bbox_index": 9, "md5": "raw-md5-9", "smi": "CCCl", "caption": "compound 11",
         "esmi": "CCCl<sep>11", "bbox": [90, 10, 160, 60], "score": 0.72},
    ]))
    pred = json.loads(world["pred"].read_text())
    reactant = pred["anchors"][0]["selected_examples"][0]["reactants"][0]
    reactant["_source"]["molparser_md5"] = "raw-md5-8"
    reactant["_source"]["bbox_index"] = 999  # md5 wins over a conflicting index
    reactant["_source"]["derivation"] = {"method": "molparser_direct"}
    product = pred["anchors"][0]["selected_examples"][0]["products"][0]
    product["_source"]["bbox_index"] = 9
    product["_source"]["derivation"] = {"method": "rgroup_fill"}
    world["pred"].write_text(json.dumps(pred, indent=1))
    base = _import(world, collection="raw_ref",
                   extra=["--perception", str(perception)])
    base_dir = world["data"] / "jobs" / base
    base_evidence = (base_dir / "work" / "reviewer_v2.json").read_bytes()
    with_gt = _import(world, collection="raw_gt", extra=[
        "--perception", str(perception), "--with-gt", "--gt-root", str(world["gt_released"]),
    ])
    gt_dir = world["data"] / "jobs" / with_gt
    assert (gt_dir / "work" / "reviewer_v2.json").read_bytes() == base_evidence
    evidence = json.loads(base_evidence)
    row = evidence["rows"][0]
    assert row["reactants"][0]["raw_molparser"]["smi"] == "CCO"
    assert row["reactants"][0]["raw_molparser"]["joined_by"] == "md5"
    assert row["reactants"][0]["source"]["derivation_method"] == "molparser_direct"
    assert row["products"][0]["raw_molparser"]["caption"] == "compound 11"
    assert row["products"][0]["raw_molparser"]["joined_by"] == "region_bbox_index"
    assert row["products"][0]["source"]["compact"] == "R-sub"
    assert "GT_SENTINEL" not in base_evidence.decode()


def test_synthetic_provenance_matrix_covers_all_seven_classes(tmp_path):
    perception = tmp_path / "perception"
    perception.mkdir()
    (perception / "1_molparser.json").write_text(json.dumps([
        {"bbox_index": 0, "md5": "m0", "smi": "CCO", "caption": "raw", "esmi": "CCO"},
        {"bbox_index": 1, "md5": "m1", "smi": "CCO", "caption": "equivalent", "esmi": "CCO"},
        {"bbox_index": 2, "md5": "m2", "smi": "CCO", "caption": "edited", "esmi": "CCO"},
        {"bbox_index": 3, "md5": "m3", "smi": "C*", "caption": "markush", "esmi": "C*"},
    ]))
    molecules = [
        {"identifier": "raw", "E-SMILES": "CCO",
         "_source": {"molparser_md5": "m0", "derivation": {"method": "molparser_direct"}}},
        {"identifier": "equiv", "E-SMILES": "C(C)O",
         "_source": {"molparser_md5": "m1", "derivation": {"method": "deterministic_repair"}}},
        {"identifier": "edit", "E-SMILES": "CCN",
         "_source": {"molparser_md5": "m2", "derivation": {"method": "image_reasoning"}}},
        {"identifier": "rsub", "E-SMILES": "CCC",
         "_source": {"molparser_md5": "m3", "derivation": {"method": "rgroup_fill"}}},
        {"identifier": "text", "E-SMILES": "CO", "_source": {"origin": "text"}},
        {"identifier": "image", "E-SMILES": "CN", "_source": {"origin": "image"}},
        {"identifier": "unknown", "E-SMILES": "C", "_source": {}},
    ]
    pred = {"paper_id": "C", "anchors": [{
        "anchor_figure": "Scheme C", "anchor_page": 1,
        "selected_examples": [{"instance_id": "matrix", "reactants": molecules, "products": [],
                               "conditions": [[]], "metrics": []}],
    }]}
    evidence = PV._reviewer_v2_data(
        pred, perception, [{"region_id": "1"}],
    )
    got = {m["identifier"]: (m["source"]["class"], m["raw_relation"])
           for m in evidence["rows"][0]["reactants"]}
    assert got == {
        "raw": ("molparser_raw", "equals_pred"),
        "equiv": ("molparser_equivalent_rewrite", "equivalent_to_pred"),
        "edit": ("molparser_structural_edit", "differs_from_pred"),
        "rsub": ("rgroup_substitution", "differs_from_pred"),
        "text": ("text", "unavailable"),
        "image": ("image_untraced", "unavailable"),
        "unknown": ("untracked", "unavailable"),
    }


def test_reviewer_entity_identity_is_stereo_sensitive_and_extension_conservative():
    ethanol_a = PV._molecule_entity_identity("CCO")
    ethanol_b = PV._molecule_entity_identity("C(C)O")
    assert ethanol_a["basis"] == "canonical_isomeric_smiles"
    assert ethanol_a["key"] == ethanol_b["key"]
    assert ethanol_a["stereo_sensitive"] is True
    assert ethanol_a["stereo_encoded"] is False
    assert PV._molecule_entity_identity("[13CH3]C")["stereo_encoded"] is False
    assert PV._molecule_entity_identity("C[2H]")["stereo_encoded"] is False

    clockwise = PV._molecule_entity_identity("F[C@H](Cl)Br")
    anticlockwise = PV._molecule_entity_identity("F[C@@H](Cl)Br")
    assert clockwise["key"] and anticlockwise["key"]
    assert clockwise["key"] != anticlockwise["key"]
    assert clockwise["stereo_encoded"] is True

    tagged = "*c1ccccc1<sep><a>0:R[1]</a>"
    same_tagged = PV._molecule_entity_identity(tagged)
    other_extension = PV._molecule_entity_identity(
        "*c1ccccc1<sep><a>0:R[2]</a>",
    )
    assert same_tagged["basis"] == "exact_extended_smiles_wire"
    assert same_tagged["key"] == PV._molecule_entity_identity(tagged)["key"]
    assert same_tagged["key"] != other_extension["key"]
    assert same_tagged["wire_sha256"] == hashlib.sha256(tagged.encode()).hexdigest()

    invalid = PV._molecule_entity_identity("not a molecule<sep><a>0:R[1]</a>")
    assert invalid["key"] is None
    assert invalid["basis"] == "unavailable"
    assert invalid["reason"] == "base_parse_failed"
    empty_extension = PV._molecule_entity_identity("CCO<sep>")
    assert empty_extension["key"] is None
    assert empty_extension["reason"] == "empty_extension"

    future_identity = {
        "schema": "chemistry_entity_identity_v2",
        "key": "chem:v2:externally-reviewed:abc",
        "basis": "reviewed_graph",
    }
    preserved = PV._attach_reviewer_entity_identities({
        "rows": [{
            "reactants": [{
                "smi": "CCO",
                "entity_key": future_identity["key"],
                "entity_identity": future_identity,
            }],
            "products": [],
        }],
    })
    assert preserved["rows"][0]["reactants"][0]["entity_identity"] == future_identity


def test_fixture_d_nested_whole_page_perception_join_is_page_scoped(tmp_path):
    perception = tmp_path / "perception"
    (perception / "pages").mkdir(parents=True)
    (perception / "regions").mkdir()
    (perception / "pages" / "page_002_molparser.json").write_text(json.dumps([
        {"bbox_index": 0, "md5": "page-2-md5", "smi": "CCO", "bbox": [1, 2, 3, 4]},
        {"bbox_index": 1, "md5": "duplicated-md5", "smi": "CCN", "bbox": [2, 3, 4, 5]},
    ]))
    (perception / "pages" / "page_003_molparser.json").write_text(json.dumps([
        {"bbox_index": 0, "md5": "page-3-md5", "smi": "CCCl", "bbox": [5, 6, 7, 8]},
        {"bbox_index": 1, "md5": "duplicated-md5", "smi": "CCBr", "bbox": [6, 7, 8, 9]},
    ]))
    (perception / "regions" / "7_molparser.json").write_text(json.dumps([
        {"bbox_index": 4, "md5": "region-md5", "smi": "CCC", "bbox": [9, 10, 11, 12]},
    ]))
    molecules = [
        {"identifier": "md5-new-key", "E-SMILES": "CCO",
         "_source": {"origin": "image", "md5": "page-2-md5",
                     "bbox_page": 2, "bbox_index": 99}},
        {"identifier": "page-index", "E-SMILES": "CCBr",
         "_source": {"origin": "image", "md5": "duplicated-md5",
                     "bbox_page": 3, "bbox_index": 1}},
        {"identifier": "region-index", "E-SMILES": "CCC",
         "_source": {"origin": "image", "bbox_index": 4}},
        {"identifier": "ambiguous-fails-closed", "E-SMILES": "CCN",
         "_source": {"origin": "image", "md5": "duplicated-md5", "bbox_index": 88}},
    ]
    pred = {"paper_id": "D", "anchors": [{
        "anchor_figure": "Scheme D", "anchor_page": 3,
        "selected_examples": [{"instance_id": "nested", "reactants": molecules,
                               "products": [], "conditions": [], "metrics": []}],
    }]}
    evidence = PV._reviewer_v2_data(pred, perception, [{"region_id": "7"}])
    by_id = {m["identifier"]: m for m in evidence["rows"][0]["reactants"]}
    assert by_id["md5-new-key"]["raw_molparser"]["joined_by"] == "md5"
    page = by_id["page-index"]
    assert page["raw_molparser"]["smi"] == "CCBr"
    assert page["raw_molparser"]["joined_by"] == "page_bbox_index"
    assert page["source"]["join_warning"] == "md5_ambiguous:2"
    assert by_id["region-index"]["raw_molparser"]["joined_by"] == "region_bbox_index"
    ambiguous = by_id["ambiguous-fails-closed"]
    assert ambiguous["raw_molparser"] is None
    assert ambiguous["source"]["join_warning"] == "md5_ambiguous:2"


def test_reviewer_condition_and_yield_diagnostics_are_semantic_and_numeric():
    role_only = IMP._condition_diagnostic(
        ["reagent: LiTMP", "reagent: I2"],
        ["base: LiTMP", "oxidant: I2 (1.2 equiv)"],
    )
    assert role_only["status"] == "pass"
    assert {item["kind"] for item in role_only["info"]} <= {
        "role_only", "formatting_only", "extra_detail", "semantic_match"}

    numeric = IMP._condition_diagnostic(
        ["temperature: -20 C", "time: 2 h"],
        ["temperature: 20 C", "time: 4 h"],
    )
    assert numeric["status"] == "diff"
    assert sum(item["kind"] == "numeric_mismatch"
               for item in numeric["substantive"]) == 2

    assert IMP._yield_value("77 g (45%)") == "|45||%"
    assert IMP._yield_value("≤50% resolution") == "<=|50||%"
    assert IMP._yield_value("45–56 %") == "|45|56|%"


def test_with_gt_reviewed_layout(world):
    job_id = _import(world, collection="rev",
                     extra=["--with-gt", "--gt-root", str(world["gt_reviewed"])])
    cmp_d = json.loads((world["data"] / "jobs" / job_id / "gt" / "compare.json").read_text())
    assert cmp_d["summary"]["matched"] == 1 and cmp_d["summary"]["n_gt"] == 3


# ---------------------------------------------------------------- bundle + endpoints
def test_bundle_zip_import(world, tmp_path):
    z = tmp_path / "run50.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(world["pdf"], "main.pdf")
        zf.write(world["pred"], "pred.json")
        zf.write(world["score"], "score.jsonl")
    job_id = IMP.main(["--bundle", str(z), "--data-root", str(world["data"]),
                       "--collection", "bz", "--no-reviewer"])
    assert (world["data"] / "jobs" / job_id / "work" / "main.pdf").exists()


def test_server_compare_endpoints(world, monkeypatch):
    no_gt = _import(world, collection="a")
    gt = _import(world, collection="b", extra=["--with-gt", "--gt-root", str(world["gt_released"])])
    monkeypatch.setattr(HS, "JOBS_DIR", world["data"] / "jobs")
    m0 = HS.api_pdf_meta(no_gt)
    assert m0["gt_attached"] is False and "reviewer_base" in m0 and m0["validate"] is not None
    with pytest.raises(HTTPException) as e:
        HS.api_pdf_compare(no_gt)
    assert e.value.status_code == 404
    m1 = HS.api_pdf_meta(gt)
    assert m1["gt_attached"] is True
    c = HS.api_pdf_compare(gt)
    assert c["summary"]["matched"] == 1 and c["schema"] == "pdf_compare_v1"


def test_adjudication_sidecar_is_atomic_and_source_bytes_are_immutable(world, monkeypatch):
    job_id = _import(world, collection="adjudicate", extra=[
        "--with-gt", "--gt-root", str(world["gt_released"]),
    ])
    job = world["data"] / "jobs" / job_id
    protected = {
        path: path.read_bytes()
        for path in (job / "result.json", job / "work" / "pdf_view.json",
                     job / "work" / "reviewer_v2.json", job / "gt" / "gt_paper.json",
                     job / "gt" / "compare.json")
    }
    monkeypatch.setattr(HS, "JOBS_DIR", world["data"] / "jobs")
    monkeypatch.setattr(HS, "LOCAL_RUNTIME_USER_ID", IMP.LOCAL_IMPORT_OWNER)

    class Request:
        cookies = {}

        async def json(self):
            return {"items": [{
                "difference_id": "pred:0:0",
                "verdict": "OCSR error",
                "note": "raw trace confirms the discrepancy",
                "correction": {"smiles": "CCN"},
            }]}

    saved = asyncio.run(HS.api_pdf_save_adjudications(job_id, Request()))
    assert saved["items"][0]["verdict"] == "OCSR error"
    assert HS.api_pdf_adjudications(job_id) == saved
    sidecar = job / "review" / "adjudications.json"
    assert sidecar.exists() and "GT_SENTINEL" not in sidecar.read_text()
    for path, before in protected.items():
        assert path.read_bytes() == before, path
    assert not list((job / "review").glob("*.tmp"))


def test_first_version_migrates_and_undo_restores_legacy_adjudications(
    world,
    monkeypatch,
):
    job_id = _import(world, collection="legacy-v0")
    job = world["data"] / "jobs" / job_id
    monkeypatch.setattr(HS, "JOBS_DIR", world["data"] / "jobs")
    monkeypatch.setattr(HS, "LOCAL_RUNTIME_USER_ID", IMP.LOCAL_IMPORT_OWNER)
    protected = {
        path: path.read_bytes()
        for path in (
            job / "result.json",
            job / "work" / "pdf_view.json",
            job / "work" / "reviewer_v2.json",
            job / "work" / "main.pdf",
        )
    }
    legacy = {
        "difference_id": "pred:0:0",
        "verdict": "OCSR error",
        "note": "legacy human verdict must survive version migration",
        "correction": {"smiles": "CCN"},
        "updated_at": 1.0,
    }
    review_dir = job / "review"
    review_dir.mkdir()
    (review_dir / "adjudications.json").write_text(
        json.dumps({"schema": "pdf_adjudications_v1", "items": [legacy]}),
        encoding="utf-8",
    )

    initial = HS.api_pdf_review_versions(job_id)
    assert initial["head_version"] == 0
    assert initial["adjudications"] == [legacy]
    assert not (review_dir / "baseline-v0.json").exists(), "GET/preview must stay read-only"
    proposal = HS._pdf_review_proposal(job, {
        "base_version": 0,
        "after": {
            "difference_id": "pred:0:0",
            "verdict": "correct fields",
            "note": "versioned product correction",
            "correction": {"condition_text": "80 °C"},
            "updated_at": 2.0,
        },
        "proposed_at": "2026-07-31T00:00:00Z",
    })
    assert proposal["before"] == legacy
    assert not (review_dir / "baseline-v0.json").exists()

    class ApplyRequest:
        cookies = {}

        async def json(self):
            return {"proposal": proposal}

    applied = asyncio.run(HS.api_pdf_review_apply(job_id, ApplyRequest()))
    assert applied["head_version"] == 1
    assert applied["adjudications"][0]["verdict"] == "correct fields"
    baseline = json.loads((review_dir / "baseline-v0.json").read_text())
    assert baseline["schema"] == "chemrxn.pdf-review-baseline.v1"
    assert baseline["adjudications"] == [legacy]
    assert baseline["patch_id"] == proposal["base_patch_id"]

    class UndoRequest:
        cookies = {}

        async def json(self):
            return {"head_version": 1}

    undone = asyncio.run(HS.api_pdf_review_undo(job_id, UndoRequest()))
    assert undone["head_version"] == 0
    assert undone["adjudications"] == [legacy]
    assert json.loads((review_dir / "adjudications.json").read_text())["items"] == [legacy]
    for path, before in protected.items():
        assert path.read_bytes() == before, path

    baseline_path = review_dir / "baseline-v0.json"
    baseline_bytes = baseline_path.read_bytes()
    tampered = json.loads(baseline_bytes)
    tampered["adjudications"][0]["note"] = "tampered"
    baseline_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        HS.api_pdf_review_versions(job_id)
    assert error.value.status_code == 409
    baseline_path.write_bytes(baseline_bytes)


def test_pre_baseline_review_chain_upgrades_without_rewriting_old_versions(
    world,
    monkeypatch,
):
    job_id = _import(world, collection="pre-baseline-chain")
    job = world["data"] / "jobs" / job_id
    monkeypatch.setattr(HS, "JOBS_DIR", world["data"] / "jobs")
    monkeypatch.setattr(HS, "LOCAL_RUNTIME_USER_ID", IMP.LOCAL_IMPORT_OWNER)
    protected = {
        path: path.read_bytes()
        for path in (
            job / "result.json",
            job / "work" / "pdf_view.json",
            job / "work" / "reviewer_v2.json",
            job / "work" / "main.pdf",
        )
    }
    review_dir = job / "review"
    review_dir.mkdir()
    source = HS._pdf_review_source_hashes(job)
    after = {
        "difference_id": "pred:0:0",
        "verdict": "correct fields",
        "note": "created by the pre-baseline implementation",
        "correction": {"condition_text": "80 °C"},
        "updated_at": 1.0,
    }
    proposal = {
        "schema": "chemrxn.proposed-review-patch.v1",
        "reaction_id": "pred:0:0",
        "base_version": 0,
        "base_patch_id": "sealed-prediction",
        "source": source,
        "operation": "review_correction",
        "before": None,
        "after": after,
        "proposed_by": "local reviewer",
        "proposed_at": "2026-07-30T00:00:00Z",
        "raw_prediction_mutated": False,
    }
    proposal["proposal_id"] = HS._pdf_review_proposal_digest(proposal)
    old_snapshot = {
        "schema": "chemrxn.pdf-reviewed-snapshot.v1",
        "version": 1,
        "patch_id": f"reviewed-v1:{proposal['proposal_id'][:12]}",
        "parent_version": 0,
        "reaction_id": "pred:0:0",
        "operation": "review_correction",
        "verdict": after["verdict"],
        "correction": after["correction"],
        "note": after["note"],
        "actor": "local reviewer",
        "created_at": "2026-07-30T00:00:01Z",
        "base_patch_id": "sealed-prediction",
        "raw_prediction_mutated": False,
        "source": source,
        "proposal": proposal,
        "adjudications": [after],
        "geometry_corrections": [],
    }
    (review_dir / "reviewed-v1.json").write_text(json.dumps(old_snapshot))
    (review_dir / "diff-v1.json").write_text(json.dumps({
        "schema": "chemrxn.pdf-review-diff.v1",
        "version": 1,
        "proposal": proposal,
    }))
    (review_dir / "current.json").write_text(json.dumps({
        "schema": "chemrxn.pdf-review-head.v1",
        "head_version": 1,
        "updated_at": 1.0,
    }))
    (review_dir / "adjudications.json").write_text(json.dumps({
        "schema": "pdf_adjudications_v1",
        "items": [after],
    }))
    old_snapshot_bytes = (review_dir / "reviewed-v1.json").read_bytes()

    upgraded = HS.api_pdf_review_versions(job_id)
    assert upgraded["head_version"] == 1
    assert upgraded["adjudications"] == [after]
    assert upgraded["head_patch_id"] == old_snapshot["patch_id"]
    assert not (review_dir / "baseline-v0.json").exists(), "GET upgrade remains read-only"

    class UndoRequest:
        cookies = {}

        async def json(self):
            return {"head_version": 1}

    undone = asyncio.run(HS.api_pdf_review_undo(job_id, UndoRequest()))
    assert undone["head_version"] == 0
    assert undone["adjudications"] == []
    baseline = json.loads((review_dir / "baseline-v0.json").read_text())
    assert baseline["migration"] == "pre-baseline-version-chain"
    assert baseline["patch_id"] == "sealed-prediction"
    assert (review_dir / "reviewed-v1.json").read_bytes() == old_snapshot_bytes
    for path, before in protected.items():
        assert path.read_bytes() == before, path


def test_only_legacy_vocabulary_can_preserve_an_unmatched_difference_id(
    world,
    monkeypatch,
):
    job_id = _import(world, collection="legacy-id-boundary")
    job = world["data"] / "jobs" / job_id
    monkeypatch.setattr(HS, "JOBS_DIR", world["data"] / "jobs")
    monkeypatch.setattr(HS, "LOCAL_RUNTIME_USER_ID", IMP.LOCAL_IMPORT_OWNER)
    review_dir = job / "review"
    review_dir.mkdir()
    sidecar = review_dir / "adjudications.json"
    legacy = {
        "difference_id": "legacy:gt-only:42",
        "verdict": "prediction accepted",
        "note": "historical row identity",
        "correction": None,
        "updated_at": 1.0,
    }
    sidecar.write_text(json.dumps({
        "schema": "pdf_adjudications_v1",
        "items": [legacy],
    }))
    assert HS.api_pdf_review_versions(job_id)["adjudications"] == [legacy]

    product = {
        **legacy,
        "verdict": "accept as extracted",
    }
    sidecar.write_text(json.dumps({
        "schema": "pdf_adjudications_v1",
        "items": [product],
    }))
    with pytest.raises(HTTPException) as error:
        HS.api_pdf_review_versions(job_id)
    assert error.value.status_code == 409


def test_review_chain_validation_is_request_linear_and_version_bounded(
    world,
    monkeypatch,
):
    job_id = _import(world, collection="lineage-shape")
    job = world["data"] / "jobs" / job_id
    monkeypatch.setattr(HS, "JOBS_DIR", world["data"] / "jobs")
    monkeypatch.setattr(HS, "LOCAL_RUNTIME_USER_ID", IMP.LOCAL_IMPORT_OWNER)
    evidence = json.loads((job / "work" / "reviewer_v2.json").read_text())
    row = evidence["rows"][0]
    target_id = row["reactants"][0]["occurrence"]["occurrence_id"]

    class ApplyRequest:
        cookies = {}

        def __init__(self, proposal):
            self.proposal = proposal

        async def json(self):
            return {"proposal": self.proposal}

    for base_version in range(8):
        offset = 12 + base_version * 3
        proposal = HS._pdf_review_proposal(job, {
            "operation": "locate_pdf_geometry",
            "base_version": base_version,
            "target": {
                "difference_id": row["id"],
                "target_type": "molecule",
                "target_id": target_id,
                "target_path": ["reactants", 0],
            },
            "after": {
                "source_document": "main",
                "page": 2,
                "bbox_frame": "pdf_points",
                "bbox": [100 + offset, 100, 160 + offset, 140],
            },
            "note": f"bounded geometry correction {base_version + 1}",
            "proposed_at": f"2026-07-31T00:00:{base_version:02d}Z",
        })
        applied = asyncio.run(
            HS.api_pdf_review_apply(job_id, ApplyRequest(proposal)),
        )
        assert applied["head_version"] == base_version + 1

    original_sha256_file = HS._sha256_file
    original_pdf_page_geometry = HS._pdf_page_geometry
    original_snapshot = HS._pdf_review_snapshot
    hash_reads = []
    pdf_reads = []
    snapshot_reads = []

    def counted_sha256_file(path):
        hash_reads.append(Path(path).name)
        return original_sha256_file(path)

    def counted_pdf_page_geometry(path, scale=1.0):
        pdf_reads.append(Path(path).name)
        return original_pdf_page_geometry(path, scale)

    def counted_snapshot(jdir, version, context=None):
        snapshot_reads.append(version)
        return original_snapshot(jdir, version, context)

    monkeypatch.setattr(HS, "_sha256_file", counted_sha256_file)
    monkeypatch.setattr(HS, "_pdf_page_geometry", counted_pdf_page_geometry)
    monkeypatch.setattr(HS, "_pdf_review_snapshot", counted_snapshot)
    state = HS.api_pdf_review_versions(job_id)
    assert state["head_version"] == 8
    assert len(state["versions"]) == 8
    assert hash_reads == ["result.json", "pdf_view.json", "reviewer_v2.json", "main.pdf"]
    assert pdf_reads == ["main.pdf"]
    assert snapshot_reads == list(range(8, -1, -1))
    snapshot_sizes = []
    for version in range(1, 9):
        raw_snapshot = json.loads(
            (job / "review" / f"reviewed-v{version}.json").read_text(),
        )
        assert raw_snapshot["schema"] == "chemrxn.pdf-reviewed-snapshot.v2"
        assert raw_snapshot["state_encoding"] == "parent-plus-delta-v1"
        assert "adjudications" not in raw_snapshot
        assert "geometry_corrections" not in raw_snapshot
        snapshot_sizes.append(
            (job / "review" / f"reviewed-v{version}.json").stat().st_size,
        )
    assert max(snapshot_sizes) - min(snapshot_sizes) < 1000

    monkeypatch.setattr(HS, "_PDF_REVIEW_MAX_VERSIONS", 8)
    next_proposal = HS._pdf_review_proposal(job, {
        "operation": "locate_pdf_geometry",
        "base_version": 8,
        "target": {
            "difference_id": row["id"],
            "target_type": "molecule",
            "target_id": target_id,
            "target_path": ["reactants", 0],
        },
        "after": {
            "source_document": "main",
            "page": 2,
            "bbox_frame": "pdf_points",
            "bbox": [139, 100, 199, 140],
        },
        "note": "must fail at the explicit version bound",
        "proposed_at": "2026-07-31T00:01:00Z",
    })
    with pytest.raises(HTTPException) as error:
        asyncio.run(HS.api_pdf_review_apply(job_id, ApplyRequest(next_proposal)))
    assert error.value.status_code == 409
    assert not (job / "review" / "reviewed-v9.json").exists()

    oldest = job / "review" / "reviewed-v1.json"
    oldest_bytes = oldest.read_bytes()
    tampered = json.loads(oldest_bytes)
    tampered["proposal"]["after"]["geometry"]["bbox"][0] += 1
    oldest.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        HS.api_pdf_review_versions(job_id)
    assert error.value.status_code == 409
    oldest.write_bytes(oldest_bytes)


def test_distinct_review_edits_use_linear_delta_validation(world, monkeypatch):
    job_id = _import(world, collection="distinct-delta-shape")
    job = world["data"] / "jobs" / job_id
    monkeypatch.setattr(HS, "JOBS_DIR", world["data"] / "jobs")
    monkeypatch.setattr(HS, "LOCAL_RUNTIME_USER_ID", IMP.LOCAL_IMPORT_OWNER)
    result_path = job / "result.json"
    result = json.loads(result_path.read_text())
    prototype = result["anchors"][0]["selected_examples"][0]
    result["anchors"][0]["selected_examples"] = [
        {**json.loads(json.dumps(prototype)), "instance_id": str(index + 1)}
        for index in range(24)
    ]
    result_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    protected = {
        path: path.read_bytes()
        for path in (
            result_path,
            job / "work" / "pdf_view.json",
            job / "work" / "reviewer_v2.json",
            job / "work" / "main.pdf",
        )
    }

    class ApplyRequest:
        cookies = {}

        def __init__(self, proposal):
            self.proposal = proposal

        async def json(self):
            return {"proposal": self.proposal}

    for base_version in range(24):
        proposal = HS._pdf_review_proposal(job, {
            "base_version": base_version,
            "after": {
                "difference_id": f"pred:0:{base_version}",
                "verdict": "accept as extracted",
                "note": f"distinct review item {base_version + 1}",
                "correction": None,
                "updated_at": float(base_version + 1),
            },
            "proposed_at": f"2026-07-31T00:02:{base_version:02d}Z",
        })
        state = asyncio.run(
            HS.api_pdf_review_apply(job_id, ApplyRequest(proposal)),
        )
        assert state["head_version"] == base_version + 1

    original_clean = HS._clean_product_review_item
    clean_calls = []

    def counted_clean(item):
        clean_calls.append((item or {}).get("difference_id"))
        return original_clean(item)

    monkeypatch.setattr(HS, "_clean_product_review_item", counted_clean)
    state = HS.api_pdf_review_versions(job_id)
    assert state["head_version"] == 24
    assert len(state["adjudications"]) == 24
    assert clean_calls == [f"pred:0:{index}" for index in range(24)]
    sizes = [
        (job / "review" / f"reviewed-v{version}.json").stat().st_size
        for version in range(1, 25)
    ]
    assert max(sizes) - min(sizes) < 1000
    assert sum(sizes) < 24 * 6000
    for path, before in protected.items():
        assert path.read_bytes() == before, path


def test_locate_on_pdf_is_bounded_versioned_and_source_immutable(world, monkeypatch):
    job_id = _import(world, collection="locate")
    job = world["data"] / "jobs" / job_id
    protected = {
        path: path.read_bytes()
        for path in (
            job / "result.json",
            job / "work" / "pdf_view.json",
            job / "work" / "reviewer_v2.json",
            job / "work" / "main.pdf",
        )
    }
    evidence = json.loads((job / "work" / "reviewer_v2.json").read_text())
    molecule = evidence["rows"][0]["reactants"][0]
    target_id = molecule["occurrence"]["occurrence_id"]
    monkeypatch.setattr(HS, "JOBS_DIR", world["data"] / "jobs")
    monkeypatch.setattr(HS, "LOCAL_RUNTIME_USER_ID", IMP.LOCAL_IMPORT_OWNER)
    correction = {
        "operation": "locate_pdf_geometry",
        "base_version": 0,
        "target": {
            "difference_id": evidence["rows"][0]["id"],
            "target_type": "molecule",
            "target_id": target_id,
            "target_path": ["reactants", 0],
        },
        "after": {
            "source_document": "main",
            "page": 2,
            "bbox_frame": "pdf_points",
            "bbox": [110, 110, 170, 150],
            # The server must ignore any client-authored original location.
            "original": {"bbox": [0, 0, 1, 1]},
        },
        "note": "molecule box moved after source review",
        "proposed_at": "2026-07-30T00:00:00Z",
    }
    proposal = HS._pdf_review_proposal(job, correction)
    assert proposal["operation"] == "locate_pdf_geometry"
    geometry = proposal["after"]
    assert geometry["occurrence_id"] == target_id
    assert geometry["original"] == {
        "source_document": "main",
        "page": 2,
        "bbox_frame": "pdf_points",
        "bbox": [100.0, 100.0, 160.0, 140.0],
    }

    class ApplyRequest:
        cookies = {}

        async def json(self):
            return {"proposal": proposal}

    reviewer_source = job / "work" / "reviewer_v2.json"
    reviewer_before = reviewer_source.read_bytes()
    reviewer_source.write_bytes(reviewer_before + b"\n")
    with pytest.raises(HTTPException) as error:
        asyncio.run(HS.api_pdf_review_apply(job_id, ApplyRequest()))
    assert error.value.status_code == 409
    assert not (job / "review").exists()
    reviewer_source.write_bytes(reviewer_before)

    main_pdf = job / "work" / "main.pdf"
    main_pdf_before = main_pdf.read_bytes()
    main_pdf.write_bytes(main_pdf_before + b"\n")
    with pytest.raises(HTTPException) as error:
        asyncio.run(HS.api_pdf_review_apply(job_id, ApplyRequest()))
    assert error.value.status_code == 409
    assert not (job / "review").exists()
    main_pdf.write_bytes(main_pdf_before)

    applied = asyncio.run(HS.api_pdf_review_apply(job_id, ApplyRequest()))
    geometry_v1 = {**geometry, "reviewed_version": 1}
    assert applied["head_version"] == 1
    assert applied["versions"][0]["operation"] == "locate_pdf_geometry"
    assert applied["geometry_corrections"] == [geometry_v1]
    assert applied["adjudications"] == []
    snapshot = json.loads((job / "review" / "reviewed-v1.json").read_text())
    assert snapshot["schema"] == "chemrxn.pdf-reviewed-snapshot.v2"
    assert snapshot["state_encoding"] == "parent-plus-delta-v1"
    assert "geometry_corrections" not in snapshot
    assert "adjudications" not in snapshot
    assert json.loads((job / "review" / "diff-v1.json").read_text())[
        "proposal"
    ]["after"] == geometry
    for path, before in protected.items():
        assert path.read_bytes() == before, path

    result_source = job / "result.json"
    result_before = result_source.read_bytes()
    result_source.write_bytes(result_before + b"\n")
    with pytest.raises(HTTPException) as error:
        HS.api_pdf_review_versions(job_id)
    assert error.value.status_code == 409
    result_source.write_bytes(result_before)

    snapshot_path = job / "review" / "reviewed-v1.json"
    snapshot_before = snapshot_path.read_bytes()
    tampered_snapshot = json.loads(snapshot_before)
    tampered_snapshot["version"] = 99
    snapshot_path.write_text(json.dumps(tampered_snapshot))
    with pytest.raises(HTTPException) as error:
        HS.api_pdf_review_versions(job_id)
    assert error.value.status_code == 409
    snapshot_path.write_bytes(snapshot_before)

    tampered_snapshot = json.loads(snapshot_before)
    tampered_snapshot["proposal"]["after"]["geometry"]["bbox"][2] = 9999
    snapshot_path.write_text(json.dumps(tampered_snapshot))
    with pytest.raises(HTTPException) as error:
        HS.api_pdf_review_versions(job_id)
    assert error.value.status_code == 409
    snapshot_path.write_bytes(snapshot_before)

    tampered_snapshot = json.loads(snapshot_before)
    tampered_snapshot["geometry_corrections"] = []
    snapshot_path.write_text(json.dumps(tampered_snapshot))
    with pytest.raises(HTTPException) as error:
        HS.api_pdf_review_versions(job_id)
    assert error.value.status_code == 409
    snapshot_path.write_bytes(snapshot_before)

    product = evidence["rows"][0]["products"][0]
    second_target_id = product["occurrence"]["occurrence_id"]
    second_correction = {
        "operation": "locate_pdf_geometry",
        "base_version": 1,
        "target": {
            "difference_id": evidence["rows"][0]["id"],
            "target_type": "molecule",
            "target_id": second_target_id,
            "target_path": ["products", 0],
        },
        "after": {
            "source_document": "main",
            "page": 2,
            "bbox_frame": "pdf_points",
            "bbox": [210, 110, 270, 150],
        },
        "note": "second occurrence on the same reaction remains independent",
        "proposed_at": "2026-07-30T00:01:00Z",
    }
    second_proposal = HS._pdf_review_proposal(job, second_correction)
    competing_correction = json.loads(json.dumps(second_correction))
    competing_correction["after"]["bbox"] = [215, 115, 275, 155]
    competing_correction["note"] = "competing reviewer at the same head"
    competing_proposal = HS._pdf_review_proposal(job, competing_correction)

    class SecondApplyRequest:
        cookies = {}

        async def json(self):
            return {"proposal": second_proposal}

    original_exclusive_json = HS._exclusive_json
    exclusive_calls = []

    def fail_snapshot_publish(path, payload):
        exclusive_calls.append(path)
        if len(exclusive_calls) == 2:
            raise OSError("simulated snapshot publish failure")
        return original_exclusive_json(path, payload)

    monkeypatch.setattr(HS, "_exclusive_json", fail_snapshot_publish)
    with pytest.raises(OSError, match="simulated snapshot"):
        asyncio.run(HS.api_pdf_review_apply(job_id, SecondApplyRequest()))
    assert not (job / "review" / "reviewed-v2.json").exists()
    assert not (job / "review" / "diff-v2.json").exists()
    assert HS.api_pdf_review_versions(job_id)["head_version"] == 1
    monkeypatch.setattr(HS, "_exclusive_json", original_exclusive_json)

    applied_second = asyncio.run(
        HS.api_pdf_review_apply(job_id, SecondApplyRequest())
    )
    assert applied_second["head_version"] == 2
    assert len(applied_second["geometry_corrections"]) == 2
    assert {
        item["occurrence_id"]
        for item in applied_second["geometry_corrections"]
    } == {target_id, second_target_id}
    assert {
        item["reaction_id"]
        for item in applied_second["geometry_corrections"]
    } == {evidence["rows"][0]["id"]}

    class CompetingApplyRequest:
        cookies = {}

        async def json(self):
            return {"proposal": competing_proposal}

    with pytest.raises(HTTPException) as error:
        asyncio.run(HS.api_pdf_review_apply(job_id, CompetingApplyRequest()))
    assert error.value.status_code == 409
    assert HS.api_pdf_review_versions(job_id)["head_version"] == 2

    no_op = json.loads(json.dumps(correction))
    no_op["base_version"] = 2
    no_op["after"]["bbox"] = [110, 110, 170, 150]
    no_op["after"].pop("original", None)
    with pytest.raises(HTTPException) as error:
        HS._pdf_review_proposal(job, no_op)
    assert error.value.status_code == 409

    outside = json.loads(json.dumps(correction))
    outside["base_version"] = 2
    outside["after"]["bbox"] = [110, 110, 700, 150]
    outside["after"].pop("original", None)
    with pytest.raises(HTTPException) as error:
        HS._pdf_review_proposal(job, outside)
    assert error.value.status_code == 400

    forged = json.loads(json.dumps(correction))
    forged["base_version"] = 2
    forged["target"]["target_id"] = "main:a0:r0:reactant:forged"
    forged["after"].pop("original", None)
    with pytest.raises(HTTPException) as error:
        HS._pdf_review_proposal(job, forged)
    assert error.value.status_code == 400

    class UndoRequest:
        cookies = {}

        async def json(self):
            return {"head_version": 2}

    undone = asyncio.run(HS.api_pdf_review_undo(job_id, UndoRequest()))
    assert undone["head_version"] == 1
    assert undone["geometry_corrections"] == [geometry_v1]
    assert (job / "review" / "reviewed-v2.json").is_file()

    class FinalUndoRequest:
        cookies = {}

        async def json(self):
            return {"head_version": 1}

    undone = asyncio.run(HS.api_pdf_review_undo(job_id, FinalUndoRequest()))
    assert undone["head_version"] == 0
    assert (job / "review" / "reviewed-v1.json").is_file()
    for path, before in protected.items():
        assert path.read_bytes() == before, path


def test_reviewer_gt_sidecars_written_only_with_flag(world):
    argv = ["--pdf", str(world["pdf"]), "--pred", str(world["pred"]),
            "--data-root", str(world["data"]), "--collection", "rv",
            "--with-gt", "--gt-root", str(world["gt_released"])]
    IMP.main(argv)                                        # reviewer mirror ON (default)
    rstore = world["data"] / "reviewer"
    slugs = [p for p in rstore.iterdir() if p.is_dir()]
    assert slugs, "reviewer mirror should have created anchor tasks"
    sidecars = list(rstore.rglob("0_predicted_output.reviewed.json"))
    assert sidecars, "matched GT regions must land as reviewer sidecars"
    # sidecars live in the REVIEWER store only, never inside the job
    assert not list((world["data"] / "jobs").rglob("*reviewed*"))


# ---------------------------------------------------------------- view geometry honors bbox_page/frame
def test_bbox_page_and_frame_honored(world):
    """A pred molecule declaring a NON-pdf bbox_frame is a contract violation (the frozen
    validator errors on it), but a --force import must still fail CLOSED: the box is never drawn
    as page points, and the reason is recorded per anchor."""
    bad = json.loads(world["pred"].read_text())
    bad["anchors"][0]["selected_examples"][0]["products"][0]["_source"] = {
        "origin": "image", "type": "image_structure",
        "bbox": [1000, 100, 1400, 300], "bbox_frame": "crop_px_unnormalized"}
    world["pred"].write_text(json.dumps(bad, indent=1))
    job_id = _import(world, collection="geo", extra=["--force"])
    view = json.loads((world["data"] / "jobs" / job_id / "work" / "pdf_view.json").read_text())
    mols = {(m["anchor"], m["side"]): m for m in view["molecules"]}
    # reactant declared bbox_frame=pdf_points on bbox_page 2 -> placed there
    assert mols[(0, "reactants")]["page"] == 2 and mols[(0, "reactants")]["source"] == "pred"
    # product declared crop_px frame -> NEVER drawn, reason recorded on the anchor
    assert (0, "products") not in mols
    a0 = view["anchors"][0]
    assert a0["molecules_located"] == 1 and a0["molecules_unlocated"] == 1
    assert any("bbox_frame" in k for k in (a0.get("unlocated_reasons") or {}))


# ---------------------------------------------------------------- launcher smoke (no services)
LAUNCHER = SCRIPTS / "local_workbench.sh"


def test_shared_reviewer_is_top_level_and_source_is_normal_git():
    reviewer = REPO.parent / "reviewer"
    server = reviewer / "chem_reviewer" / "server.py"
    assert server.is_file()
    assert not server.read_bytes().startswith(b"version https://git-lfs")
    assert HS.REVIEWER_ROOT.resolve() == reviewer.resolve()
    assert HS._KETCHER_DIR.resolve() == (reviewer / "vendor" / "ketcher").resolve()
    assert not (REPO / "viewer").exists()


def _sh(args, **kw):
    return subprocess.run(["bash", str(LAUNCHER), *args], capture_output=True, text=True,
                          timeout=60, **kw)


def test_launcher_and_importer_share_local_owner_default():
    launcher = LAUNCHER.read_text()
    assert 'LOCAL_OWNER="${CHEMRXN_LOCAL_RUNTIME_USER_ID:-local-user}"' in launcher
    assert 'CHEMRXN_LOCAL_RUNTIME_USER_ID="$LOCAL_OWNER"' in launcher
    assert IMP.LOCAL_IMPORT_OWNER == "local-user"


def test_direct_pdf_replay_owns_the_full_workspace_column():
    """A sealed ?job= replay has no runtime session or Pi pane.

    Keep the direct document on the sole grid track; otherwise the permanent
    new-task shell places <main> on a 5px resizer track while its hidden
    history/agent columns consume the viewport.
    """
    html = (REPO / "service" / "harness_index.html").read_text()
    selector = "body.local-product.runtime-pdf-workspace:not(.runtime-session)"
    assert f"{selector} #history" in html
    assert f"{selector} #agentpane" in html
    assert f"{selector} #app{{" in html
    assert "grid-template-columns:minmax(0,1fr)!important" in html
    assert f"{selector} #app>main{{" in html
    assert "grid-column:1;width:100%;min-width:0" in html


def test_launcher_rebuilds_runtime_instead_of_reusing_stale_skill_pins():
    launcher = LAUNCHER.read_text()
    assert "runtime_skills_valid()" in launcher
    assert "loadPinnedSkillManifest(process.argv[1])" in launcher
    assert 'if ! runtime_skills_valid; then' in launcher
    assert '(cd "$REPO/runtime/pi-service" && npm run build' in launcher
    assert 'if [ ! -f "$REPO/runtime/pi-service/dist/src/server.js" ]' not in launcher


def test_launcher_status_and_clear_cache_on_empty_root(tmp_path):
    r = _sh(["status", "--data-root", str(tmp_path)])
    assert r.returncode == 0 and "stopped" in r.stdout
    r2 = _sh(["clear-cache", "--data-root", str(tmp_path)])
    assert r2.returncode == 0


def test_launcher_fails_clearly_on_lfs_pointer_reviewer(tmp_path):
    fake = tmp_path / "rev"
    fake.mkdir()
    (fake / "server.py").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 1\n")
    r = _sh(["start", "--data-root", str(tmp_path / "d"), "--reviewer-dir", str(fake),
             "--port", "18999", "--reviewer-port", "18998", "--runtime-port", "18997",
             "--python", sys.executable])
    assert r.returncode != 0
    assert "lfs pull" in (r.stdout + r.stderr)
    # Preflight must finish before any service starts; failed setup cannot leave
    # an orphaned runtime or reviewer behind.
    run = tmp_path / "d" / "run"
    assert not any((run / f"{name}.pid").exists()
                   for name in ("runtime", "reviewer", "viewer"))
