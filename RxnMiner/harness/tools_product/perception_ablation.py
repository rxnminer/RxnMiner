#!/usr/bin/env python3
"""Perception ablation: which source covers the GT molecules better, per figure?

Perception is the accuracy ceiling. For each of the 35 pdf_benchmark figures we
compute the molecule-set RECALL of each perception source against the GT molecule
set (all canon SMILES across GT selected_examples + reaction_templates):

  - UniParser   (molecules parsed from <N>_*uniparser_raw.json)
  - MolParser   (caption>smi from <N>_*molparser_p1.json)
  - union       (UniParser ∪ MolParser)

No API calls — reads the two perception caches written by the runners. Stereo is
stripped (matches the headline F1 mode). Markush GT mols (with `*`) keep their
dummies on both sides so a perceived Markush template can still match a GT one.

  agent/.venv/bin/python tools_product/perception_ablation.py
"""
from __future__ import annotations

import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools_product"))
from uniparser_to_v2 import parse_objects, canon  # noqa: E402

BENCH = os.path.join(REPO, "pdf_benchmark")


def gt_mols(gt: dict) -> set:
    out = set()
    pools = (gt.get("selected_examples") or []) + (gt.get("reaction_templates") or [])
    for r in pools:
        for side in ("reactants", "products"):
            for m in (r.get(side) or []):
                c = canon(m.get("E-SMILES") or m.get("smiles") or "")
                if c:
                    out.add(c)
    return out


def up_mols(raw_path: str) -> set:
    if not os.path.exists(raw_path):
        return None  # not perceived (cache absent)
    raw = json.load(open(raw_path))
    out = set()
    for o in parse_objects(raw):
        if o.kind == "molecule" and o.payload:
            c = canon(o.payload)
            if c:
                out.add(c)
    return out


def mp_mols(mp_path: str) -> set:
    if not os.path.exists(mp_path):
        return None
    det = json.load(open(mp_path))
    out = set()
    for d in det:
        c = canon(d.get("caption") or d.get("smi") or "")
        if c:
            out.add(c)
    return out


def cov(perceived, gt):
    if perceived is None or not gt:
        return None
    return len(perceived & gt) / len(gt)


def main():
    gts = sorted(g for g in glob.glob(f"{BENCH}/*/*.reviewed.json") if "diff" not in g)
    print(f"{'figure':52s} {'GTm':>4s} {'UPm':>4s} {'MPm':>4s} "
          f"{'cUP':>5s} {'cMP':>5s} {'cUNI':>5s}")
    agg = {"gt": 0, "up_hit": 0, "mp_hit": 0, "uni_hit": 0,
           "up_seen": 0, "mp_seen": 0, "n": 0}
    for g in gts:
        rel = os.path.relpath(g, BENCH)
        gt = json.load(open(g))
        G = gt_mols(gt)
        UP = up_mols(g.replace(".reviewed.json", ".uniparser_raw.json"))
        MP = mp_mols(g.replace(".reviewed.json", ".molparser_p1.json"))
        UNI = (UP or set()) | (MP or set()) if (UP is not None or MP is not None) else None
        cu, cm, cn = cov(UP, G), cov(MP, G), cov(UNI, G)
        print(f"{rel[:52]:52s} {len(G):>4d} "
              f"{(len(UP) if UP is not None else -1):>4d} "
              f"{(len(MP) if MP is not None else -1):>4d} "
              f"{('%.2f'%cu if cu is not None else '  - '):>5s} "
              f"{('%.2f'%cm if cm is not None else '  - '):>5s} "
              f"{('%.2f'%cn if cn is not None else '  - '):>5s}")
        if G:
            agg["n"] += 1
            agg["gt"] += len(G)
            if UP is not None:
                agg["up_hit"] += len(UP & G); agg["up_seen"] += len(UP)
            if MP is not None:
                agg["mp_hit"] += len(MP & G); agg["mp_seen"] += len(MP)
            if UNI is not None:
                agg["uni_hit"] += len(UNI & G)
    print("-" * 80)
    gtn = max(1, agg["gt"])
    print(f"CORPUS  GT_mols={agg['gt']}  over {agg['n']} figs")
    print(f"  UniParser  molecule-recall = {agg['up_hit']/gtn:.3f}  "
          f"(saw {agg['up_seen']} mols, {agg['up_hit']} in GT)")
    print(f"  MolParser  molecule-recall = {agg['mp_hit']/gtn:.3f}  "
          f"(saw {agg['mp_seen']} mols, {agg['mp_hit']} in GT)")
    print(f"  UNION      molecule-recall = {agg['uni_hit']/gtn:.3f}  "
          f"<- perception ceiling for the deterministic path")


if __name__ == "__main__":
    main()
