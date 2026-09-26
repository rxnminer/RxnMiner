"""Group-meeting results figure: our agent vs the bare Gemini-3.1-Pro baseline across
the 3 figure-level benchmarks (GT1, GT2, pdf_benchmark), under the loose metric others
use (Jaccard>=0.5) AND the strict metric we care about (exact SMILES + stereo).

Numbers are pulled live from the scorer outputs (no hand-typed values):
  GT1/GT2: _external/benchmark_v2_3svc/predictions/external/_unified_4method_scores.json
  pdf:     tools_product/pdf_4method_scores.json   (tools_product/score_pdf_4methods.py)

  agent/.venv/bin/python tools_product/make_scoreboard_figure.py
  -> figures/scoreboard_3benchmarks.png
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parent.parent
GT = json.load(open(REPO / "_external/benchmark_v2_3svc/predictions/external/_unified_4method_scores.json"))
PDF = json.load(open(REPO / "tools_product/pdf_4method_scores.json"))

# (benchmark label, baseline (method,score-dict), our-best (label, score-dict))
def g(bench, sysname):
    return GT[bench][sysname]

ROWS = [
    ("GT1\n(140 figs)",
     g("gt1", "gemini-3.1-pro-on-gt1"),
     "ChemAgent", g("gt1", "opencode-chemagent-dedup-on-gt1")),       # 109/140; filebased run in progress
    ("GT2\n(78 figs)",
     g("gt2", "gemini-3.1-pro-on-gt2"),
     "ChemAgent", g("gt2", "opencode-chemagent-filebased-on-gt2")),   # 77/78
    ("pdf_benchmark\n(35 figs)",
     PDF["bare-Gemini"],
     "our pipeline", PDF["our-pipeline_G+MP"]),
]

BASE = "#9aa0a6"      # grey baseline
OURS = "#c2693a"      # copper-orange (repo accent)


def f1(d, m):
    return d[m]["f1"]


def panel(ax, method_key, title, ours_label_once):
    labels = [r[0] for r in ROWS]
    base = [f1(r[1], method_key) for r in ROWS]
    ours = [f1(r[3], method_key) for r in ROWS]
    x = np.arange(len(labels)); w = 0.36
    b1 = ax.bar(x - w/2, base, w, label="bare Gemini-3.1-Pro (1-shot)", color=BASE)
    b2 = ax.bar(x + w/2, ours, w, label="our agent", color=OURS)
    for bars in (b1, b2):
        for rect in bars:
            h = rect.get_height()
            ax.annotate(f"{h:.3f}", (rect.get_x()+rect.get_width()/2, h),
                        ha="center", va="bottom", fontsize=8.5)
    # per-group method note for ours
    for xi, r in zip(x, ROWS):
        ax.annotate(r[2], (xi + w/2, 0.005), ha="center", va="bottom",
                    fontsize=7, color="white", rotation=90)
    ax.axhline(0.9, ls="--", lw=0.9, color="#444", alpha=0.6)
    ax.annotate("0.9 target", (len(labels)-0.5, 0.905), ha="right", va="bottom",
                fontsize=8, color="#444")
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylim(0, 1.0); ax.set_ylabel("reaction-level F1")
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.grid(axis="y", ls=":", alpha=0.4)


fig, (axL, axR) = plt.subplots(1, 2, figsize=(12, 5.2), sharey=True)
panel(axL, "A_jaccard05", "Loose metric — Jaccard ≥ 0.5\n(what other papers report)", True)
panel(axR, "C_strict_stereo", "Strict metric — exact SMILES + stereo\n(what we hold ourselves to)", False)
axL.legend(loc="upper left", fontsize=8.5, framealpha=0.9)

fig.suptitle("Reaction extraction from figures: our agent vs bare Gemini-3.1-Pro baseline",
             fontsize=13, fontweight="bold", y=0.99)
fig.text(0.5, 0.005,
         "Independent ChemEAGLE GT (GT1/GT2): our agent wins on every metric, including strict+stereo. "
         "pdf_benchmark GT is provenance-tied to Gemini (caveat). GT1 ChemAgent = 109/140 (full run in progress).",
         ha="center", fontsize=8, color="#555")
fig.tight_layout(rect=[0, 0.03, 1, 0.96])

out = REPO / "figures" / "scoreboard_3benchmarks.png"
out.parent.mkdir(exist_ok=True)
fig.savefig(out, dpi=150, bbox_inches="tight")
print(f"-> {out}")

# also dump the exact numbers used
tbl = {}
for label, based, ourslabel, oursd in ROWS:
    key = label.split("\n")[0]
    tbl[key] = {"baseline_bareGemini": {m: round(based[m]["f1"], 4) for m in ["A_jaccard05", "C_strict_stereo"]},
                f"ours_{ourslabel}": {m: round(oursd[m]["f1"], 4) for m in ["A_jaccard05", "C_strict_stereo"]}}
(REPO / "figures" / "scoreboard_numbers.json").write_text(json.dumps(tbl, indent=2))
print(json.dumps(tbl, indent=2))
