"""The evaluation layer (agent/score_v2.py + tools_product/score_paper.norm_mol).

GT comparison is POST-HOC and never touches the agent loop (no leakage). These tests check the
canonical set matching used by the vs-GT scorecard."""
import json

import pytest

from conftest import REPO

from tools_product.score_paper import norm_mol
import score_v2 as SV


def test_norm_mol_strips_markush_markup():
    assert norm_mol("c1ccccc1<sep>", True) == "c1ccccc1"
    # markush placeholder is kept as a dummy atom (not None), so it can be excluded by '*' downstream
    out = norm_mol("*C(O)C<sep><a>0:R</a>", True)
    assert out is not None and "*" in out


def test_norm_mol_canonicalizes_representation():
    # two representations of toluene -> same canonical (the whole point of canonical matching)
    assert norm_mol("Cc1ccccc1", False) == norm_mol("c1ccccc1C", False)


def test_smiles_accuracy_perfect_self():
    v2 = {"selected_examples": [{"reactants": [{"E-SMILES": "CCO"}],
                                 "products": [{"E-SMILES": "CC=O"}]}],
          "reaction_templates": []}
    sa = SV.smiles_accuracy(v2, v2, stereo=False)
    assert sa["recall"] == 1.0 and sa["precision"] == 1.0 and sa["f1"] == 1.0
    assert sa["by_role"]["products"]["recall"] == 1.0


def test_smiles_accuracy_partial_overlap_has_expected_f1():
    """Independent pred vs gt (not self-vs-self): 1 of 2 predicted molecules is right.
    gt {CCO, CC=O} vs pred {CCO, c1ccccc1} -> inter=1, recall=1/2, precision=1/2, f1=0.5."""
    gt = {"selected_examples": [{"reactants": [{"E-SMILES": "CCO"}],
                                 "products": [{"E-SMILES": "CC=O"}]}], "reaction_templates": []}
    pred = {"selected_examples": [{"reactants": [{"E-SMILES": "CCO"}],
                                   "products": [{"E-SMILES": "c1ccccc1"}]}], "reaction_templates": []}
    sa = SV.smiles_accuracy(pred, gt, stereo=False)
    assert sa["recall"] == 0.5 and sa["precision"] == 0.5 and sa["f1"] == 0.5
    assert sa["by_role"]["reactants"]["f1"] == 1.0     # reactant matched
    assert sa["by_role"]["products"]["f1"] == 0.0      # product wrong


def test_smiles_accuracy_canonicalizes_before_comparing():
    """Same molecule written differently must still match (the scorer canonicalizes, not string-compares)."""
    gt = {"selected_examples": [{"reactants": [{"E-SMILES": "c1ccccc1C"}], "products": []}],
          "reaction_templates": []}
    pred = {"selected_examples": [{"reactants": [{"E-SMILES": "Cc1ccccc1"}], "products": []}],
            "reaction_templates": []}
    assert SV.smiles_accuracy(pred, gt, stereo=False)["f1"] == 1.0


def test_smiles_accuracy_partial():
    gt = {"selected_examples": [{"reactants": [{"E-SMILES": "CCO"}],
                                 "products": [{"E-SMILES": "CC=O"}, {"E-SMILES": "CCC"}]}],
          "reaction_templates": []}
    pred = {"selected_examples": [{"reactants": [{"E-SMILES": "CCO"}],
                                   "products": [{"E-SMILES": "CC=O"}]}],   # missed CCC
            "reaction_templates": []}
    sa = SV.smiles_accuracy(pred, gt, stereo=False)
    assert sa["n_gt"] == 3 and sa["n_correct"] == 2
    assert round(sa["recall"], 3) == 0.667 and sa["precision"] == 1.0


GT47 = REPO / "pdf_benchmark" / "47__10.1021_acsorginorgau.5c00111" / "1_output.reviewed.json"


@pytest.mark.skipif(not GT47.exists(), reason="benchmark GT not present")
def test_score_v2_self_is_high():
    gt = json.loads(GT47.read_text())
    r = SV.score_v2(gt, gt)                          # scoring GT against itself
    assert r["r"] > 0.5 and r["gate"]["valid"] is True
    assert r["smiles_accuracy"]["nostereo"]["f1"] == 1.0
