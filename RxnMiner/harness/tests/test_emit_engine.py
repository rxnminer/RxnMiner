"""The emit engine (skills/chem_extract/engine/v2.py) — provenance tag + assemble/validate.

This is the deterministic gate: it canonicalizes every SMILES, stamps _source.origin, and
produces the V2 the product renders. We test the origin tagging directly and a full emit-full."""
import json
import subprocess

from conftest import PY, SKILL


def _mol_entry(*a, **k):
    from engine.v2 import _mol_entry as f          # the live engine
    return f(*a, **k)


def test_origin_molparser_from_md5():
    e, ns, nb = _mol_entry({"smiles": "CSc1ccccc1", "identifier": "2a",
                            "bbox_index": 6, "molparser_md5": "abc"}, "r0", True, [], "tag")
    assert e["_source"]["origin"] == "molparser"
    assert e["_source"]["bbox_index"] == 6
    assert e["E-SMILES"] == "CSc1ccccc1"           # canonicalized, '*'-free
    assert ns == 0 and nb == 0


def test_origin_markush_from_flag():
    e, ns, nb = _mol_entry({"smiles": "*c1ccccc1", "molparser_markush": True}, "r0", True, [], "tag")
    assert e["_source"]["origin"] == "markush"
    assert ns == 1                                  # still-markush flagged


def test_origin_agent_when_no_detection():
    e, ns, nb = _mol_entry({"smiles": "CCO", "identifier": "x"}, "r0", True, [], "tag")
    assert e["_source"]["origin"] == "agent"        # model-built, no detection signal
    assert nb == 0


def test_conditions_keep_drawn_reagent_smiles_and_bbox():
    """A DRAWN reagent/ligand in conditions (e.g. an L-series ligand in an optimization table) is
    schema-compatible: {role,text,smiles,bbox} -> {role,text,E-SMILES (canon'd),_source.bbox}.
    Regression for the s4 ligand-table case where L1/L2/L3 were kept as bare text labels."""
    from engine.v2 import _conditions
    out = _conditions([[{"role": "ligand", "text": "L1 (15 mol%)",
                         "smiles": "c1ccc(-c2ccccn2)nc1", "bbox": [100, 200, 260, 320]},
                        {"role": "reagent", "text": "KOH"}]])
    lig = out[0][0]
    assert lig["role"] == "ligand" and lig["text"] == "L1 (15 mol%)"
    assert lig["E-SMILES"] == "c1ccc(-c2ccccn2)nc1"          # canonicalized drawn structure kept
    assert lig["_source"]["bbox"] == [100, 200, 260, 320]    # bbox provenance kept
    assert "E-SMILES" not in out[0][1]                        # a plain text reagent stays text-only


def test_emit_full_flags_drawn_reagent_smiles_coverage(tmp_path):
    """emit-full must PRESERVE a drawn-ligand condition's structure+bbox and report
    drawn-reagent-smiles=Y in the verdict (it was =n on the real s4 runs)."""
    plan = {
        "is_multistep": False,
        "_meta": {"task_id": "t", "paper_id": "s4", "anchor_image": "s4",
                  "anchor_figure": "Table 1", "anchor_page": 1,
                  "reaction_class": "optimization", "molparser_detection_count": 8,
                  "identifier_map": [], "texts": []},
        "selected_examples": [{
            "label": "entry1",
            "reactants": [{"smiles": "Nc1ccccc1CO", "identifier": "1a", "bbox_index": 6}],
            "products": [{"smiles": "c1ccc(C2CCc3ccccc3N2)cc1", "identifier": "3a"}],
            "conditions": [[{"role": "catalyst", "text": "Mo(CO)6"},
                            {"role": "ligand", "text": "L1", "smiles": "c1ccc(-c2ccccn2)nc1",
                             "bbox": [100, 200, 260, 320]},
                            {"role": "solvent", "text": "Toluene"}]],
            "metrics": {"yield": "73%"}}],
        "reaction_templates": [], "reaction_graph": []}
    pp = tmp_path / "plan.json"; pp.write_text(json.dumps(plan))
    out = tmp_path / "out.json"
    p = subprocess.run([str(PY), str(SKILL / "tool.py"), "emit-full", str(pp), str(out)],
                       capture_output=True, text=True, timeout=120)
    assert out.exists(), p.stderr[-600:]
    assert "drawn-reagent-smiles=Y" in (p.stdout + p.stderr), (p.stdout + p.stderr)[-400:]
    v2 = json.loads(out.read_text())
    lig = [c for row in v2["selected_examples"][0]["conditions"] for c in row if c.get("role") == "ligand"][0]
    assert lig["E-SMILES"] == "c1ccc(-c2ccccn2)nc1" and lig["_source"]["bbox"] == [100, 200, 260, 320]


def test_emit_full_produces_valid_v2(tmp_path, sample_plan):
    pp = tmp_path / "plan.json"
    pp.write_text(json.dumps(sample_plan))
    out = tmp_path / "out.json"
    p = subprocess.run([str(PY), str(SKILL / "tool.py"), "emit-full", str(pp), str(out)],
                       capture_output=True, text=True, timeout=120)
    assert out.exists(), p.stderr[-600:]
    v2 = json.loads(out.read_text())
    ex = v2["selected_examples"][0]
    # reactant came from a detection -> origin molparser + bbox_index preserved
    assert ex["reactants"][0]["_source"]["origin"] == "molparser"
    assert ex["reactants"][0]["_source"]["bbox_index"] == 0
    # product was model-built -> agent
    assert ex["products"][0]["_source"]["origin"] == "agent"
    assert "VALID" in (p.stdout + p.stderr)
