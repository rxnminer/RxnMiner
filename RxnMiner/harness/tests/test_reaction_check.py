"""GT-FREE template-conformance check (scripts/reaction_check.py): each example must contain its template's
scaffold ('*' = wildcard). Catches a product/reactant that is the WRONG core, with no ground truth."""
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
import reaction_check as RC


def _v2(template, examples):
    return {"reaction_templates": [template], "selected_examples": examples}


def test_conforming_examples_pass():
    # template: aryl-substituted acetophenone core *C(=O)c1ccccc1 ; examples vary the aryl '*'
    t = {"reactants": [{"E-SMILES": "*Br"}, {"E-SMILES": "*C(=O)c1ccccc1"}],
         "products": [{"E-SMILES": "*C(=O)c1ccccc1"}]}
    exs = [{"instance_id": "1", "reactants": [{"E-SMILES": "CBr"}, {"E-SMILES": "CC(=O)c1ccccc1"}],
            "products": [{"E-SMILES": "CC(=O)c1ccccc1"}]},
           {"instance_id": "2", "reactants": [{"E-SMILES": "CCBr"}, {"E-SMILES": "CCC(=O)c1ccccc1"}],
            "products": [{"E-SMILES": "CCC(=O)c1ccccc1"}]}]
    r = RC.reaction_check(_v2(t, exs))
    assert r["applicable"] and r["conform_rate"] == 1.0 and not r["flagged"]


def test_off_template_product_flagged():
    # an example whose product is a completely different ring system -> product_off_template, GT-free
    t = {"reactants": [{"E-SMILES": "*C(=O)c1ccccc1"}], "products": [{"E-SMILES": "*C(=O)c1ccccc1"}]}
    exs = [{"instance_id": "good", "reactants": [{"E-SMILES": "CC(=O)c1ccccc1"}],
            "products": [{"E-SMILES": "CC(=O)c1ccccc1"}]},
           {"instance_id": "bad", "reactants": [{"E-SMILES": "CC(=O)c1ccccc1"}],
            "products": [{"E-SMILES": "c1ccncc1"}]}]                     # pyridine, not the acetophenone core
    r = RC.reaction_check(_v2(t, exs))
    flagged = {f["id"]: f["status"] for f in r["flagged"]}
    assert flagged.get("bad") == "product_off_template" and "good" not in flagged


def test_off_template_reactant_flagged():
    t = {"reactants": [{"E-SMILES": "*c1ccc(C(=O)O)cc1"}], "products": [{"E-SMILES": "*c1ccc(C(=O)OC)cc1"}]}
    exs = [{"instance_id": "bad", "reactants": [{"E-SMILES": "CCO"}],                 # not the benzoic-acid core
            "products": [{"E-SMILES": "Cc1ccc(C(=O)OC)cc1"}]}]
    r = RC.reaction_check(_v2(t, exs))
    assert any(f["status"] == "reactant_off_template" for f in r["flagged"])


def test_no_template_not_applicable():
    r = RC.reaction_check({"selected_examples": [{"instance_id": "1"}]})
    assert r["applicable"] is False


def test_harness_tool_and_emit_verdict_integration():
    """reaction_check is wired as an agent tool AND folded into the emit_full verdict (the GT-free self-check
    the agent sees every emit)."""
    import sys, pathlib, tempfile
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agent"))
    import chem_harness as H
    assert "reaction_check" in H.TOOLS
    plan = {"reaction_templates": [{"products": [{"E-SMILES": "*C(=O)c1ccccc1"}],
                                    "reactants": [{"E-SMILES": "*C(=O)c1ccccc1"}]}],
            "selected_examples": [{"instance_id": "bad", "products": [{"E-SMILES": "c1ccncc1"}],
                                   "reactants": [{"E-SMILES": "CC(=O)c1ccccc1"}]}]}
    out = H.t_reaction_check(plan)["result"]
    assert out["n_product_off"] == 1 and out["flagged"][0]["status"] == "product_off_template"
    v = H.t_emit_full(plan, tempfile.mktemp(suffix=".json"))["result"]["verdict_report"]
    assert "TEMPLATE CHECK" in v
