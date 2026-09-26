#!/usr/bin/env python3
"""Annotation viz: per-figure v1(agent) vs v2(GT+agent hybrid) with provenance/flag highlighting.
Reuses render_v2's RDKit rendering; adds colored badges so annotators see what to review.

  single fig:  python render_compare.py <fig.png> <v1.v2.json> <v2_annotation.json> out.html
  batch:       python render_compare.py --batch --delivery <DELIVERY_dir> --out <html_out_dir>
               (reads images/ v1_agent/ v2_annotation/ INDEX.csv; writes <id>.html + index.html)
"""
import sys, os, json, base64, html, csv, argparse
from rdkit import Chem, RDLogger
from rdkit.Chem.Draw import rdMolDraw2D
RDLogger.DisableLog("rdApp.*")
W, H = 190, 140

BADGE = {  # _prov / _flag -> (label, color)
    "gt":               ("GT 可信", "#0e9b8c"),
    "stereo_fill":      ("agent 补立体", "#2b7de9"),
    "reactant_fill":    ("agent 补反应物", "#2b7de9"),
    "conflict":         ("冲突·待裁决", "#e08a00"),
    "agent_candidate":  ("agent 候选·待核实", "#8b5cf6"),
}


def _svg(smi):
    s = (smi or "").split("<sep>")[0].strip()
    if not s:
        return '<span class="dim">—</span>'
    m = Chem.MolFromSmiles(s)
    if m is None:
        return '<code class="bad">%s</code>' % html.escape(s[:30])
    d = rdMolDraw2D.MolDraw2DSVG(W, H)
    rdMolDraw2D.PrepareAndDrawMolecule(d, m)
    d.FinishDrawing()
    return d.GetDrawingText().replace('<?xml version="1.0" encoding="UTF-8"?>\n', "")


def _side(lst):
    cells = []
    for mm in (lst or []):
        for comp in (mm.get("E-SMILES") or "").split("<sep>"):
            comp = comp.strip()
            if comp:
                cells.append('<div class="mol">%s<code>%s</code></div>' % (_svg(comp), html.escape(comp[:34])))
    return '<span class="plus">+</span>'.join(cells) or '<span class="dim">—</span>'


def _conds(e):
    out = []
    for step in (e.get("conditions") or []):
        for c in (step or []):
            if isinstance(c, dict) and (c.get("text") or c.get("E-SMILES")):
                out.append(html.escape(str(c.get("text") or c.get("E-SMILES"))))
    return " · ".join(out)


def _yld(e):
    for m in (e.get("metrics") or []):
        if isinstance(m, dict) and str(m.get("type") or m.get("role") or "").lower() == "yield":
            return html.escape(str(m.get("content") or m.get("text") or "").strip())
    return ""


def _badges(e):
    b = []
    prov = e.get("_prov")
    if prov in BADGE:
        lbl, col = BADGE[prov]
        b.append('<span class="badge" style="background:%s">%s</span>' % (col, lbl))
    for k, v in (e.get("_flag") or {}).items():
        if v in BADGE:
            lbl, col = BADGE[v]
            b.append('<span class="badge" style="background:%s">%s</span>' % (col, lbl))
    alt = e.get("_agent_reactants_alt")
    if alt:
        b.append('<span class="alt">agent备选: %s</span>' % html.escape(" + ".join(a[:24] for a in alt)))
    return "".join(b)


def _rxn(e, idx):
    prov = e.get("_prov", "")
    flags = e.get("_flag") or {}
    cls = "cand" if prov == "agent_candidate" else ("conf" if flags.get("reactants") == "conflict" else "ok")
    c = _conds(e)
    return ('<div class="rxn %s"><div class="n">%d %s</div>'
            '<div class="s">%s</div><div class="ar">&rarr;%s</div><div class="s">%s</div>'
            '<div class="y">%s</div></div>'
            % (cls, idx, _badges(e), _side(e.get("reactants")),
               ('<div class="c">%s</div>' % c) if c else "", _side(e.get("products")), _yld(e)))


def render_one(fig_png, v1_path, v2_path, out):
    figb64 = base64.b64encode(open(fig_png, "rb").read()).decode() if fig_png and os.path.exists(fig_png) else None
    v1 = json.load(open(v1_path)) if v1_path and os.path.exists(v1_path) else {}
    v2 = json.load(open(v2_path))
    notes = v2.get("_annotation_notes") or {}
    fid = os.path.basename(v2_path).replace(".json", "")
    v2_rows = "".join(_rxn(e, i + 1) for i, e in enumerate(v2.get("selected_examples") or []))
    v1_rows = "".join(_rxn(e, i + 1) for i, e in enumerate(v1.get("selected_examples") or []))
    fig = ('<div class="fig"><img src="data:image/png;base64,%s"></div>' % figb64) if figb64 else ""
    summary = ("匹配 %s · 立体补 %s · 反应物补 %s · <b style='color:#e08a00'>冲突 %s</b> · <b style='color:#8b5cf6'>候选 %s</b>"
               % (notes.get("matched", "?"), notes.get("stereo_fill", 0), notes.get("reactant_fill", 0),
                  notes.get("conflicts", 0), notes.get("agent_candidates", 0)))
    doc = HTML % {"fid": html.escape(fid), "summary": summary, "fig": fig,
                  "v2": v2_rows or '<div class="dim">无</div>',
                  "v1": v1_rows or '<div class="dim">无</div>'}
    open(out, "w").write(doc)
    return fid


HTML = """<!doctype html><meta charset="utf-8"><title>%(fid)s</title><style>
body{font:14px system-ui,'PingFang SC',sans-serif;margin:0;background:#f4f6f8;color:#1a2530}
header{background:#141b24;color:#e6edf4;padding:12px 20px}header b{color:#5ad1c4}
.summary{color:#c3d0dc;font-size:13px;margin-top:4px}
h2{font-size:14px;margin:18px 20px 6px;color:#33475b}
.fig{margin:12px 20px}.fig img{max-width:100%%;border:1px solid #d8e0e8;border-radius:8px;background:#fff}
.legend{margin:8px 20px;font-size:12px}.legend .badge{margin-right:6px}
.rxn{display:grid;grid-template-columns:150px 1fr auto 1fr 54px;gap:10px;align-items:center;
  background:#fff;border:1px solid #e2e8ee;border-radius:8px;margin:6px 20px;padding:7px}
.rxn.cand{background:#faf5ff;border-color:#d9c2f5}.rxn.conf{background:#fff8ec;border-color:#f0d59a}
.n{font-size:11px;color:#5a6b7a;display:flex;flex-direction:column;gap:3px}
.badge{color:#fff;border-radius:4px;padding:1px 5px;font-size:10px;white-space:nowrap;width:fit-content}
.alt{font-size:10px;color:#8b5cf6}
.s{display:flex;flex-wrap:wrap;gap:4px;align-items:center;justify-content:center}
.mol{border:1px solid #eef2f6;border-radius:6px;text-align:center;padding:1px}
.mol code{display:block;font-size:8px;color:#8ea0b2;max-width:190px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.plus{color:#8ea0b2;font-weight:700}.ar{color:#33475b;font-size:20px;text-align:center;min-width:110px}
.c{font-size:10px;color:#5a6b7a;max-width:170px}.y{font-family:ui-monospace,monospace;font-weight:700;color:#0e9b8c;text-align:right}
.dim{color:#8ea0b2;margin:6px 20px}.bad{color:#b00;font-size:9px}code.bad{background:#fee}
</style>
<header><b>标注对照</b> — %(fid)s<div class="summary">%(summary)s</div></header>
<div class="legend"><span class="badge" style="background:#0e9b8c">GT可信</span>
<span class="badge" style="background:#2b7de9">agent补立体/反应物</span>
<span class="badge" style="background:#e08a00">冲突·裁决</span>
<span class="badge" style="background:#8b5cf6">候选·核实</span></div>
%(fig)s
<h2>版本2 · 给标注同学(GT打底 + agent补，按此标注）</h2>%(v2)s
<h2>版本1 · 纯 agent 提取（参考）</h2>%(v1)s
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--delivery")
    ap.add_argument("--out")
    ap.add_argument("pos", nargs="*")
    a = ap.parse_args()
    if not a.batch:
        fig, v1, v2, out = a.pos
        print("wrote", render_one(fig, v1, v2, out))
        return
    d = a.delivery
    outdir = a.out
    os.makedirs(outdir, exist_ok=True)
    rows = list(csv.DictReader(open(os.path.join(d, "INDEX.csv"))))
    links = []
    for r in rows:
        fid = r["id"]
        fig = os.path.join(d, "images", fid + ".png")
        v1 = os.path.join(d, "v1_agent", fid + ".v2.json")
        v2 = os.path.join(d, "v2_annotation", fid + ".json")
        if not os.path.exists(v2):
            continue
        render_one(fig if os.path.exists(fig) else None, v1, v2, os.path.join(outdir, fid + ".html"))
        links.append((fid, r))
    idx = ["<!doctype html><meta charset=utf-8><title>balanced-300 标注对照</title>",
           "<style>body{font:14px system-ui,'PingFang SC';margin:20px;background:#f4f6f8}"
           "a{color:#2b7de9;text-decoration:none}tr:hover{background:#eef4fb}"
           "td,th{padding:5px 10px;border-bottom:1px solid #e2e8ee;text-align:left;font-size:13px}"
           "th{color:#33475b}</style>",
           "<h2>balanced-300 标注对照（按需复核量排序）</h2>",
           "<table><tr><th>图</th><th>难度</th><th>反应类</th><th>v1 F1</th>"
           "<th style='color:#e08a00'>冲突</th><th style='color:#8b5cf6'>候选</th><th>立体补</th></tr>"]
    for fid, r in links:
        idx.append("<tr><td><a href='%s.html'>%s</a></td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td>%s</td><td>%s</td><td>%s</td></tr>"
                   % (fid, fid.replace("-with-examples", ""), r["difficulty"], r["reaction_class"],
                      r["v1_f1_nostereo"], r["n_conflicts"], r["n_candidates"], r["n_stereo_fill"]))
    idx.append("</table>")
    open(os.path.join(outdir, "index.html"), "w").write("\n".join(idx))
    print("batch: %d figs -> %s/index.html" % (len(links), outdir))


if __name__ == "__main__":
    main()
