"""Build a self-contained review folder for the 19-fig GT2 head-to-head so the user
can eyeball the test samples: each figure image + GT vs bareGemini vs opencode vs
newChemagent reactions, RDKit-rendered, with per-fig C/A F1 and a badge on each
newChemagent reactant (recovered via 反推 / still markush / drawn-concrete).

  agent/.venv/bin/python tools_product/build_h2h_review.py
  -> h2h_gt2_review/index.html  (+ figures/<pid>.png)
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from eval.score_unified_4methods import load_gt  # GT2 variants -> reactions

from rdkit import Chem, RDLogger
from rdkit.Chem.Draw import rdMolDraw2D
RDLogger.DisableLog("rdApp.*")

PRED = REPO / "_external/benchmark_v2_3svc/predictions/external/gt2"
BENCH_IMG = REPO / "_external/chemeagle_bench/Benchmark"
OUT = REPO / "h2h_gt2_review"
SCORES = json.load(open(REPO / "tools_product/headtohead_gt2_scores.json"))
SAMPLE = json.load(open("/tmp/gt2_sample.json"))

SYS = [("GT", None), ("bareGemini", "gemini-3.1-pro-on-gt2"),
       ("opencode", "opencode-chemagent-filebased-on-gt2"),
       ("newChemagent", "chemagent-gemini-on-gt2")]

_TAG = re.compile(r"<a>[^<]*</a>|<r>[^<]*</r>|<dum>")
_svg_cache: dict = {}


def clean_smi(es: str) -> str:
    """E-SMILES -> renderable SMILES (keep '*' so markush is visible)."""
    if not es:
        return ""
    parts = es.split("<sep>")
    comps = []
    for p in parts:
        p = _TAG.sub("", p)
        p = re.sub(r":\d+", "", p)
        p = p.strip()
        if p:
            comps.append(p)
    return ".".join(comps)


def mol_svg(smi: str, w=120, h=95) -> str:
    if not smi:
        return '<span class="bad">∅</span>'
    key = (smi, w, h)
    if key in _svg_cache:
        return _svg_cache[key]
    m = Chem.MolFromSmiles(smi, sanitize=True)
    if m is None:
        m = Chem.MolFromSmiles(smi, sanitize=False)
    if m is None:
        out = f'<span class="bad" title="{html.escape(smi)}">⚠ {html.escape(smi[:24])}</span>'
        _svg_cache[key] = out
        return out
    try:
        d = rdMolDraw2D.MolDraw2DSVG(w, h)
        d.drawOptions().bondLineWidth = 1
        rdMolDraw2D.PrepareAndDrawMolecule(d, m)
        d.FinishDrawing()
        svg = d.GetDrawingText().replace("<?xml version='1.0' encoding='iso-8859-1'?>", "")
        out = f'<span class="mol" title="{html.escape(smi)}">{svg}</span>'
    except Exception:
        out = f'<span class="bad" title="{html.escape(smi)}">⚠</span>'
    _svg_cache[key] = out
    return out


def rxn_html(reactants, products, react_badges=None) -> str:
    rs = []
    for i, r in enumerate(reactants):
        badge = ""
        if react_badges and i < len(react_badges) and react_badges[i]:
            badge = f'<span class="badge {react_badges[i][0]}">{react_badges[i][1]}</span>'
        rs.append(mol_svg(clean_smi(r)) + badge)
    ps = [mol_svg(clean_smi(p)) for p in products]
    left = '<span class="plus">+</span>'.join(rs) or '<span class="bad">∅</span>'
    right = '<span class="plus">+</span>'.join(ps) or '<span class="bad">∅</span>'
    return f'<div class="rxn">{left}<span class="arrow">→</span>{right}</div>'


def gt_reactions(pid):
    gt = load_gt("gt2").get(pid) or []
    out = []
    for r in gt:
        rr = [x.get("smiles", "") for x in r.get("reactants", [])]
        pp = [x.get("smiles", "") for x in r.get("products", [])]
        out.append((rr, pp, None))
    return out


def sys_reactions(dname, pid):
    # newChemagent: prefer the reassembled v2 (carries _source badges)
    if dname == "chemagent-gemini-on-gt2":
        v2p = PRED / dname / f"{pid}.v2_reassembled.json"
        if not v2p.exists():
            v2p = PRED / dname / f"{pid}.v2.json"
        if not v2p.exists():
            return []
        v2 = json.loads(v2p.read_text())
        out = []
        for ex in v2.get("selected_examples", []):
            rr, badges = [], []
            for r in ex.get("reactants", []):
                es = r.get("E-SMILES", "")
                rr.append(es)
                src = r.get("_source") or {}
                if src.get("reactant_recovered"):
                    badges.append(("ok", "反推"))
                elif "*" in es or "<a>" in es:
                    badges.append(("warn", "markush"))
                else:
                    badges.append(None)
            pp = [p.get("E-SMILES", "") for p in ex.get("products", [])]
            out.append((rr, pp, badges))
        return out
    # bareGemini / opencode: parsed.reactions
    f = PRED / dname / f"{pid}.pred.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text())
    rxns = (d.get("parsed", {}) or {}).get("reactions", []) or []
    out = []
    for r in rxns:
        rr = [x if isinstance(x, str) else (x.get("E-SMILES") or x.get("smiles") or "")
              for x in r.get("reactants", [])]
        pp = [x if isinstance(x, str) else (x.get("E-SMILES") or x.get("smiles") or "")
              for x in r.get("products", [])]
        if rr or pp:
            out.append((rr, pp, None))
    return out


CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#faf8f4;color:#222}
header{background:#1f2937;color:#fff;padding:18px 26px}
h1{margin:0;font-size:20px} .sub{color:#cbd5e1;font-size:13px;margin-top:4px}
.board{margin:14px 26px;border-collapse:collapse;font-size:13px}
.board th,.board td{border:1px solid #d6d3cd;padding:5px 12px;text-align:center}
.board th{background:#eef2ff}.board td.sys{text-align:left;font-weight:600}
.figsec{margin:22px 26px;padding:16px;background:#fff;border:1px solid #e5e2dc;border-radius:10px}
.fightitle{font-size:15px;font-weight:700;margin-bottom:2px}
.figmeta{font-size:12px;color:#6b7280;margin-bottom:10px}
.figimg{max-width:760px;max-height:380px;border:1px solid #e5e2dc;border-radius:6px;margin-bottom:12px;background:#fff}
.cols{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.col{background:#fafafa;border:1px solid #ececec;border-radius:8px;padding:8px;min-width:0}
.colh{font-weight:700;font-size:13px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.f1{font-size:11px;font-weight:600;padding:1px 6px;border-radius:8px;background:#eef2ff;color:#3730a3}
.rxn{display:flex;flex-wrap:wrap;align-items:center;gap:2px;padding:4px 2px;border-bottom:1px dashed #eee}
.mol svg{vertical-align:middle}
.arrow{font-size:18px;color:#c2693a;margin:0 4px}.plus{color:#999;margin:0 2px}
.bad{color:#b91c1c;font-size:11px}
.badge{font-size:9px;padding:0 4px;border-radius:6px;margin-left:2px;vertical-align:top}
.badge.ok{background:#dcfce7;color:#166534}.badge.warn{background:#fee2e2;color:#991b1b}
.more{font-size:11px;color:#6b7280;padding:4px}
.colGT{background:#f0fdf4;border-color:#bbf7d0}
.legend{font-size:11px;color:#555;margin:6px 26px}
"""


def board_html():
    agg = SCORES["aggregate"]
    rows = ""
    for lab in ["bareGemini", "opencode", "newChemagent"]:
        a = agg[lab]
        rows += (f'<tr><td class="sys">{lab}</td>'
                 f'<td>{a["A_jaccard05"]["f1"]:.3f}</td>'
                 f'<td>{a["B_strict_ns_nosalt"]["f1"]:.3f}</td>'
                 f'<td><b>{a["C_strict_stereo"]["f1"]:.3f}</b></td>'
                 f'<td>{a["D_strict_ns"]["f1"]:.3f}</td></tr>')
    return (f'<table class="board"><tr><th>system</th><th>A · Jaccard≥0.5 (loose)</th>'
            f'<th>B · exact no-stereo</th><th>C · exact+stereo ★</th><th>D · exact no-stereo+salt</th></tr>'
            f'{rows}</table>')


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "figures").mkdir(exist_ok=True)
    perfig = {r["pid"]: r for r in SCORES["per_fig"]}
    sample = sorted(SAMPLE, key=lambda c: c["nv"])
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>GT2 head-to-head sample (19 figs)</title><style>{CSS}</style></head><body>"]
    parts.append("<header><h1>GT2 同图头对头 · 19 张分层样本</h1>"
                 "<div class='sub'>裸Gemini · opencode ChemAgent · 新chemagent &nbsp;|&nbsp; 主指标 C = 精确+手性逐 SMILES。"
                 "每张图按 GT 的 variant 数排序(表越大越难)。</div></header>")
    parts.append(board_html())
    parts.append("<div class='legend'>徽章:<span class='badge ok'>反推</span>=新chemagent 反应物经引擎反推恢复; "
                 "<span class='badge warn'>markush</span>=仍是泛式(未带入,exact 必挂)。绿底列=GT。</div>")

    for c in sample:
        pid = c["pid"]; img = c["img"]
        shutil.copy(BENCH_IMG / img, OUT / "figures" / f"{pid}.png")
        pf = perfig.get(pid, {})
        parts.append("<div class='figsec'>")
        parts.append(f"<div class='fightitle'>{html.escape(pid)}</div>")
        meta = f"variants(nGT)={c['nv']} · reactants={c['nr']} ({'bi' if c['nr']>=2 else 'mono'}-molecular)"
        parts.append(f"<div class='figmeta'>{meta}</div>")
        parts.append(f"<img class='figimg' src='figures/{html.escape(pid)}.png'>")
        parts.append("<div class='cols'>")
        for label, dname in SYS:
            if label == "GT":
                rxns = gt_reactions(pid); f1txt = f"n={len(rxns)}"
                colcls = "col colGT"
            else:
                rxns = sys_reactions(dname, pid)
                v = pf.get(label) or {}
                f1txt = (f"C={v.get('C_f1','—')} A={v.get('A_f1','—')}" if v else "—")
                colcls = "col"
            parts.append(f"<div class='{colcls}'><div class='colh'>{label}<span class='f1'>{f1txt}</span></div>")
            CAP = 26
            for (rr, pp, badges) in rxns[:CAP]:
                parts.append(rxn_html(rr, pp, badges))
            if len(rxns) > CAP:
                parts.append(f"<div class='more'>(+{len(rxns)-CAP} more)</div>")
            parts.append("</div>")
        parts.append("</div></div>")

    parts.append("</body></html>")
    (OUT / "index.html").write_text("\n".join(parts), encoding="utf-8")

    # also a compact text comparison + the scores json
    shutil.copy(REPO / "tools_product/headtohead_gt2_scores.json", OUT / "scores.json")
    readme = (
        "GT2 head-to-head — 19-fig stratified sample\n"
        "===========================================\n\n"
        "Open index.html in a browser. Per figure: the original ChemEAGLE figure image,\n"
        "then 4 columns (GT | bareGemini | opencode | newChemagent) of extracted reactions\n"
        "rendered as structures, with per-fig C (exact+stereo) / A (loose Jaccard) F1.\n\n"
        "Aggregate (corpus-pooled over 19 figs):\n"
    )
    agg = SCORES["aggregate"]
    for lab in ["bareGemini", "opencode", "newChemagent"]:
        a = agg[lab]
        readme += (f"  {lab:<13} A={a['A_jaccard05']['f1']:.3f}  B={a['B_strict_ns_nosalt']['f1']:.3f}  "
                   f"C(★)={a['C_strict_stereo']['f1']:.3f}  D={a['D_strict_ns']['f1']:.3f}\n")
    readme += ("\nKey finding: newChemagent products are ~0.90 exact, but its exact-match is gated by\n"
               "the varying REACTANT staying markush (CASE A: reactant not separately drawn). The '反推'\n"
               "badge marks reactants recovered by engine reverse-from-product (CASE B only).\n")
    (OUT / "README.txt").write_text(readme, encoding="utf-8")
    print(f"-> {OUT}/index.html   ({len(sample)} figs, {len(_svg_cache)} unique mols rendered)")


if __name__ == "__main__":
    main()
