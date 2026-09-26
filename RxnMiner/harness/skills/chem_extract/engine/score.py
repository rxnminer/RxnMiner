"""engine.score — OPTIONAL self-check. Score a V2 prediction against a ground-truth
file with the 4 evaluation methods we use. The PRIMARY metric is C (exact SMILES,
stereo kept) — that is the bar your extraction must clear.

  A_jaccard05        Jaccard>=0.5, stereo+salts stripped   (loose; what other papers use)
  B_strict_ns_nosalt exact multiset, stereo+salts stripped
  C_strict_stereo ★  exact multiset, stereo KEPT, salts kept   (strictest — OUR bar)
  D_strict_ns        exact multiset, stereo stripped, salts kept

GT file may be: a V2 doc (selected_examples/reaction_templates), a {"reactions":[...]} doc,
or a ChemEAGLE gt2 row with "variants". Reactions are matched by reactant∪product SMILES
multisets via Hungarian assignment; conditions are NOT scored (SMILES only).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO = os.environ.get("CHEM_REPO")
if not _REPO:
    for p in Path(__file__).resolve().parents:
        if (p / "eval/score_unified_4methods.py").exists():
            _REPO = str(p); break
sys.path.insert(0, _REPO)
from eval.score_unified_4methods import match_count, f1  # noqa: E402

METHODS = {"A_jaccard05": (True, True, "jaccard_05"),
           "B_strict_ns_nosalt": (True, True, "exact"),
           "C_strict_stereo": (False, False, "exact"),
           "D_strict_ns": (True, False, "exact")}


def _smis(entries):
    out = []
    for e in entries or []:
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict):
            s = e.get("E-SMILES") or e.get("smiles") or ""
            if s:
                out.append(s)
    return out


def _pool(doc):
    """Any supported doc -> [{reactants:[smi], products:[smi]}]."""
    if isinstance(doc, list):
        doc = {"reactions": doc}
    rxns = []
    if doc.get("variants"):  # chemeagle gt2 row
        for v in doc["variants"].values():
            rxns.append({"reactants": v.get("reactants", []), "products": v.get("products", [])})
        return rxns
    for ex in (doc.get("selected_examples") or []):
        rxns.append({"reactants": _smis(ex.get("reactants")), "products": _smis(ex.get("products"))})
    for t in (doc.get("reaction_templates") or []):
        rxns.append({"reactants": _smis(t.get("reactants")), "products": _smis(t.get("products"))})
    for r in (doc.get("reactions") or []):
        rxns.append({"reactants": _smis(r.get("reactants")), "products": _smis(r.get("products"))})
    return [r for r in rxns if r["reactants"] or r["products"]]


def score_pred_vs_gt(pred_path: str, gt_path: str) -> dict:
    pred = _pool(json.loads(Path(pred_path).read_text()))
    gt = _pool(json.loads(Path(gt_path).read_text()))
    out = {"n_pred": len(pred), "n_gt": len(gt)}
    for m, (ss, sa, mode) in METHODS.items():
        tp = match_count(pred, gt, strip_stereo=ss, strip_salts=sa, mode=mode)
        ff, p, r = f1(tp, len(pred), len(gt))
        out[m] = {"f1": round(ff, 3), "precision": round(p, 3), "recall": round(r, 3), "tp": tp}
    return out
