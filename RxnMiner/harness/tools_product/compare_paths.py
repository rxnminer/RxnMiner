#!/usr/bin/env python3
"""Three-way per-figure comparison: OCR-only vs old-baseline vs chempipe(comprehension).

Reads three prediction sets by suffix and the reviewed GT, prints a per-figure
nostereo-F1 table + corpus F1 for each path + per-figure winner. Quantifies how much
the COMPREHENSION layer (chempipe) adds over raw OCR (uniparser) and the old pipeline.

  agent/.venv/bin/python tools_product/compare_paths.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools_product"))
import score_paper as S  # noqa: E402

BENCH = os.path.join(REPO, "pdf_benchmark")
PATHS = [
    ("baseline", ".json"),         # old agent/pipeline.py  (the 0.588 set)
    ("ocr_uni", ".uniparser.json"),  # UniParser-native OCR->V2
    ("chempipe", ".chempipe.json"),  # comprehension pipeline
]


def fig_f1(pred_path, gt, stereo=False):
    if not os.path.exists(pred_path):
        return None
    try:
        pred = json.load(open(pred_path))
    except Exception:
        return None
    g = S.reactions_of(gt, stereo)
    p = S.reactions_of(pred, stereo)
    mm, _ = S.match(p, g)
    R = mm / max(1, len(g)); P = mm / max(1, len(p))
    f1 = 2 * R * P / max(1e-9, R + P)
    return {"f1": f1, "n_gt": len(g), "n_pred": len(p), "matched": mm}


def main():
    gts = sorted(g for g in glob.glob(f"{BENCH}/*/*.reviewed.json") if "diff" not in g)
    hdr = f"{'figure':50s} " + " ".join(f"{name:>9s}" for name, _ in PATHS) + "   winner"
    print(hdr)
    print("-" * len(hdr))
    corpus = {name: [0, 0, 0] for name, _ in PATHS}  # matched, pred, gt
    for g in gts:
        gt = json.load(open(g))
        rel = os.path.relpath(g, BENCH)
        cells = []
        best = (None, -1.0)
        for name, suf in PATHS:
            r = fig_f1(g.replace(".reviewed.json", suf), gt)
            if r is None:
                cells.append("    -  ")
            else:
                cells.append(f"{r['f1']:9.2f}")
                corpus[name][0] += r["matched"]; corpus[name][1] += r["n_pred"]; corpus[name][2] += r["n_gt"]
                if r["f1"] > best[1]:
                    best = (name, r["f1"])
        print(f"{rel[:50]:50s} " + " ".join(cells) + f"   {best[0] or '-'}")
    print("-" * len(hdr))
    print("CORPUS micro-F1 (nostereo):")
    for name, _ in PATHS:
        mm, npred, ng = corpus[name]
        R = mm / ng if ng else 0; P = mm / npred if npred else 0
        f1 = 2 * R * P / (R + P) if (R + P) else 0
        print(f"  {name:10s}  matched={mm:3d}/{ng:<3d}  R={R:.3f} P={P:.3f}  F1={f1:.3f}")


if __name__ == "__main__":
    main()
