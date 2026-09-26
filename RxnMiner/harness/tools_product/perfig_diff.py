#!/usr/bin/env python3
"""Per-FIGURE comparison of baseline / chempipe / product vs GT, with product
diagnosis metadata. Reuses score_paper's matcher so numbers line up with the
corpus scores. Read-only over cached predictions.

  agent/.venv/bin/python tools_product/perfig_diff.py
"""
from __future__ import annotations
import glob, json, os, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools_product"))
from score_paper import reactions_of, match  # noqa: E402

BENCH = os.path.join(REPO, "pdf_benchmark")
SUFFIXES = [(".json", "base"), (".chempipe.json", "chmp"), (".product.json", "prod")]


def f1_of(pred_path, gt_rxns):
    if not os.path.exists(pred_path):
        return None, 0
    try:
        pred = json.load(open(pred_path))
    except Exception:
        return None, 0
    pr = reactions_of(pred, stereo=False)
    if not pr and not gt_rxns:
        return 1.0, 0
    m, _ = match(pr, gt_rxns)
    p = m / len(pr) if pr else 0.0
    r = m / len(gt_rxns) if gt_rxns else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return f1, len(pr)


def main():
    gts = sorted(g for g in glob.glob(f"{BENCH}/*/*.reviewed.json") if "diff" not in g)
    print(f"{'figure':40s} {'gt':>3s} {'base':>5s} {'chmp':>5s} {'prod':>5s} {'Δp-c':>5s}  prod_wf / status / n / markush")
    print("-" * 130)
    tot = {"base": 0.0, "chmp": 0.0, "prod": 0.0}
    n = 0
    regress = []
    for gt_path in gts:
        d = os.path.dirname(gt_path)
        paper = os.path.basename(d).split("__")[0]
        base = os.path.basename(gt_path)
        fig = base.split("_", 1)[0]
        key = f"{paper}/fig{fig}"
        gt = json.load(open(gt_path))
        gt_rxns = reactions_of(gt, stereo=False)
        row = {}
        npred = {}
        for suf, name in SUFFIXES:
            f1, npr = f1_of(gt_path.replace(".reviewed.json", suf), gt_rxns)
            row[name] = f1
            npred[name] = npr
        prod = json.load(open(gt_path.replace(".reviewed.json", ".product.json"))) \
            if os.path.exists(gt_path.replace(".reviewed.json", ".product.json")) else {}
        wf = prod.get("_product_workflow", "-")
        st = prod.get("_product_status", "-")
        mk = prod.get("_product_markush") or {}
        mkstr = ""
        if mk:
            mkstr = f"eng={mk.get('n_engine')}/ag={mk.get('n_agent')}/po={mk.get('n_product_only')}" \
                if "n_engine" in mk else str(mk)[:40]
        b = row.get("base"); c = row.get("chmp"); p = row.get("prod")
        dpc = (p - c) if (p is not None and c is not None) else None
        def fmt(x): return f"{x:.2f}" if x is not None else "  - "
        print(f"{key:40s} {len(gt_rxns):3d} {fmt(b):>5s} {fmt(c):>5s} {fmt(p):>5s} "
              f"{(f'{dpc:+.2f}' if dpc is not None else '  - '):>5s}  "
              f"{str(wf)[:18]:18s} {str(st)[:15]:15s} e={npred.get('prod',0)} {mkstr}")
        for name in tot:
            if row.get(name) is not None:
                tot[name] += row[name]
        n += 1
        if dpc is not None and dpc < -0.05:
            regress.append((key, c, p, wf, st))
    print("-" * 130)
    print(f"{'MEAN per-fig F1':40s}     {tot['base']/n:.3f} {tot['chmp']/n:.3f} {tot['prod']/n:.3f}")
    print(f"\nProduct REGRESSED vs chempipe on {len(regress)} figs:")
    for k, c, p, wf, st in sorted(regress, key=lambda x: x[1] - x[2], reverse=True):
        print(f"  {k:28s} chmp={c:.2f} -> prod={p:.2f}  ({wf}/{st})")


if __name__ == "__main__":
    main()
