#!/usr/bin/env python3
"""Paper-level V2 scorer for the PRODUCT benchmark (pdf_benchmark/).

Scores a predicted paper (per-figure V2 JSONs) against the human-reviewed GT
(`*.reviewed.json`). Reports reaction-level precision/recall/F1 in TWO canon
modes — stereo-stripped (comparable to the figure-F1) and stereo-aware (the
product bar) — plus condition recall and reaction_graph edge recall.

A "reaction" = a (frozenset(canon reactants), frozenset(canon products)) tuple,
pooled from BOTH selected_examples AND reaction_templates so a prediction that
put a reaction in a template (instead of an example) still gets credit. Match =
combined-molecule-set Jaccard >= 0.5 (same rule as the figure benchmark), greedy.

Usage:
    score_paper.py <paper_dir>                      # one paper
    score_paper.py --all pdf_benchmark              # whole corpus
    score_paper.py <paper_dir> --pred-suffix _output.json   # custom pred file
"""
from __future__ import annotations
import argparse, glob, json, os, re, sys
from pathlib import Path

try:
    from rdkit import Chem
    from rdkit import RDLogger
    RDLogger.DisableLog("rdApp.*")
except Exception:
    Chem = None

TAG_A = re.compile(r"<a>[^<]*</a>")
TAG_R = re.compile(r"<r>[^<]*</r>")


def norm_mol(esmiles: str, stereo: bool = True):
    """E-SMILES -> canonical SMILES (or None). Strips Markush markup."""
    if not esmiles:
        return None
    s = TAG_A.sub("", esmiles)
    s = TAG_R.sub("", s)
    s = s.replace("<sep>", ".").replace("<dum>", "*")
    s = s.strip(". ").strip()
    if not s:
        return None
    if Chem is None:
        return s
    m = Chem.MolFromSmiles(s)
    if m is None:
        return s  # keep raw token so wrong-but-present still counts as a (bad) match candidate
    try:
        return Chem.MolToSmiles(m, isomericSmiles=stereo)
    except Exception:
        return s


def reactions_of(obj: dict, stereo: bool):
    """Pool reaction tuples from selected_examples + reaction_templates."""
    rxns = []
    def comp(side):
        out = set()
        for c in side or []:
            n = norm_mol(c.get("E-SMILES") or c.get("smiles") or "", stereo)
            if n:
                out.add(n)
        return out
    for e in (obj.get("selected_examples") or []):
        r, p = comp(e.get("reactants")), comp(e.get("products"))
        if r or p:
            rxns.append((frozenset(r), frozenset(p)))
    for t in (obj.get("reaction_templates") or []):
        r, p = comp(t.get("reactants")), comp(t.get("products"))
        if r or p:
            rxns.append((frozenset(r), frozenset(p)))
    # dedup
    seen, uniq = set(), []
    for rx in rxns:
        k = (rx[0], rx[1])
        if k not in seen:
            seen.add(k); uniq.append(rx)
    return uniq


def jaccard(a: frozenset, b: frozenset) -> float:
    u = a | b
    return len(a & b) / len(u) if u else (1.0 if not a and not b else 0.0)


def match(pred_rxns, gt_rxns, thr=0.5):
    """Greedy best-match by combined-molecule-set Jaccard. Returns matched count."""
    used = [False] * len(pred_rxns)
    matched = 0
    pairs = []
    for gi, (gr, gp) in enumerate(gt_rxns):
        gset = gr | gp
        best, bj = -1, 0.0
        for pi, (pr, pp) in enumerate(pred_rxns):
            if used[pi]:
                continue
            j = jaccard(pset := (pr | pp), gset)
            if j > bj:
                bj, best = j, pi
        if best >= 0 and bj >= thr:
            used[best] = True; matched += 1
            pairs.append((best, gi, bj))
    return matched, pairs


def cond_entries(obj):
    n = 0
    for e in (obj.get("selected_examples") or []):
        for step in (e.get("conditions") or []):
            n += len(step) if isinstance(step, list) else 1
    return n


def score_figure(pred: dict, gt: dict):
    res = {}
    for mode, stereo in (("nostereo", False), ("stereo", True)):
        pr, gr = reactions_of(pred, stereo), reactions_of(gt, stereo)
        m, _ = match(pr, gr)
        res[mode] = dict(n_pred=len(pr), n_gt=len(gr), matched=m)
    res["cond_pred"] = cond_entries(pred)
    res["cond_gt"] = cond_entries(gt)
    res["edge_pred"] = len(pred.get("reaction_graph") or [])
    res["edge_gt"] = len(gt.get("reaction_graph") or [])
    return res


def find_pairs(paper_dir: Path, pred_suffix: str = ".json"):
    """Yield (pred_path, gt_path) for each figure in a paper dir.

    pred_suffix lets you score an alternative prediction set non-destructively
    (e.g. ".uniparser.json" keeps the baseline ".json" predictions intact)."""
    for gt in sorted(paper_dir.glob("*.reviewed.json")):
        if "diff" in gt.name:
            continue
        pred = gt.with_name(gt.name.replace(".reviewed.json", pred_suffix))
        if pred.exists():
            yield pred, gt


def score_paper(paper_dir: Path, pred_suffix: str = ".json"):
    figs = []
    for pred_p, gt_p in find_pairs(paper_dir, pred_suffix):
        try:
            figs.append(score_figure(json.loads(pred_p.read_text()),
                                     json.loads(gt_p.read_text())))
        except Exception as e:
            print(f"  !! {pred_p.name}: {e}", file=sys.stderr)
    agg = {"figs": len(figs)}
    for mode in ("nostereo", "stereo"):
        np = sum(f[mode]["n_pred"] for f in figs)
        ng = sum(f[mode]["n_gt"] for f in figs)
        mm = sum(f[mode]["matched"] for f in figs)
        rec = mm / ng if ng else 0.0
        prec = mm / np if np else 0.0
        f1 = 2 * rec * prec / (rec + prec) if (rec + prec) else 0.0
        agg[mode] = dict(n_pred=np, n_gt=ng, matched=mm,
                         recall=round(rec, 3), precision=round(prec, 3), f1=round(f1, 3))
    agg["cond_recall"] = round(
        min(1.0, sum(f["cond_pred"] for f in figs) / max(1, sum(f["cond_gt"] for f in figs))), 3)
    agg["edge_recall"] = round(
        min(1.0, sum(f["edge_pred"] for f in figs) / max(1, sum(f["edge_gt"] for f in figs))), 3)
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="paper dir, or corpus root with --all")
    ap.add_argument("--all", action="store_true", help="treat target as corpus root")
    ap.add_argument("--pred-suffix", default=".json",
                    help="prediction filename suffix replacing '.reviewed.json' "
                         "(default '.json'; e.g. '.uniparser.json' to score an alt set)")
    args = ap.parse_args()
    root = Path(args.target)
    papers = sorted(d for d in root.glob("*") if d.is_dir()) if args.all else [root]

    print(f"{'paper':42s} {'figs':>4s} {'ns_R':>5s} {'ns_P':>5s} {'ns_F1':>5s} "
          f"{'st_F1':>5s} {'condR':>5s} {'edgeR':>5s}")
    tot = {"nostereo": [0, 0, 0], "stereo": [0, 0, 0]}  # matched, pred, gt
    for p in papers:
        a = score_paper(p, args.pred_suffix)
        if not a["figs"]:
            continue
        for m in ("nostereo", "stereo"):
            tot[m][0] += a[m]["matched"]; tot[m][1] += a[m]["n_pred"]; tot[m][2] += a[m]["n_gt"]
        print(f"{p.name[:42]:42s} {a['figs']:>4d} {a['nostereo']['recall']:>5.2f} "
              f"{a['nostereo']['precision']:>5.2f} {a['nostereo']['f1']:>5.2f} "
              f"{a['stereo']['f1']:>5.2f} {a['cond_recall']:>5.2f} {a['edge_recall']:>5.2f}")

    print("-" * 80)
    for m in ("nostereo", "stereo"):
        mm, np, ng = tot[m]
        rec = mm / ng if ng else 0; prec = mm / np if np else 0
        f1 = 2 * rec * prec / (rec + prec) if (rec + prec) else 0
        print(f"CORPUS [{m:8s}] matched={mm:4d}/{ng:<4d} gt  recall={rec:.3f} "
              f"precision={prec:.3f} F1={f1:.3f}")


if __name__ == "__main__":
    main()
