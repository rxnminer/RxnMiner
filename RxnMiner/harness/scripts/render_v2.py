#!/usr/bin/env python3
"""Standalone V2-JSON reaction visualizer — PORTABLE (only needs rdkit).

    python render_v2.py <extraction.v2.json> [out.html] [figure.png]

Reads a chem_harness V2 extraction and writes ONE self-contained HTML with every reaction
drawn (reactant structures -> products, conditions, yields, plus the markush templates).
SVGs are inlined, so the .html is fully shareable — hand the single file to anyone.

Hand-off to someone else: copy JUST this script + your `*.v2.json` (+ optional figure png).
    pip install rdkit
    python render_v2.py my.v2.json my.html my_figure.png
    open my.html
"""
import sys, json, base64, os, html
from rdkit import Chem, RDLogger
from rdkit.Chem.Draw import rdMolDraw2D
RDLogger.DisableLog("rdApp.*")

W, H = 200, 150

def placeholder(txt):
    t = html.escape((txt or "?")[:28])
    return ('<svg width="%d" height="%d" xmlns="http://www.w3.org/2000/svg">'
            '<rect width="100%%" height="100%%" fill="#fafafa" stroke="#e0b0b0"/>'
            '<text x="50%%" y="50%%" font-size="10" fill="#b00" text-anchor="middle">%s</text></svg>'
            % (W, H, t))

def svg(smi):
    s = (smi or "").split("<sep>")[0].strip()
    if not s:
        return placeholder("(none)")
    m = Chem.MolFromSmiles(s)
    if m is None:
        return placeholder(s)
    try:
        d = rdMolDraw2D.MolDraw2DSVG(W, H)
        rdMolDraw2D.PrepareAndDrawMolecule(d, m)
        d.FinishDrawing()
        return d.GetDrawingText().replace('<?xml version="1.0" encoding="UTF-8"?>\n', "")
    except Exception:
        return placeholder(s)

def side(lst):
    cells = []
    for mm in (lst or []):
        raw = mm.get("E-SMILES") or mm.get("smiles") or ""
        for comp in raw.split("<sep>"):
            comp = comp.strip()
            if not comp:
                continue
            cells.append('<div class="mol">%s<code>%s</code></div>' % (svg(comp), html.escape(comp)))
    return '<span class="plus">+</span>'.join(cells) or '<span class="dim">—</span>'

def conds(grp):
    out = []
    for step in (grp.get("conditions") or []):
        for c in (step or []):
            if isinstance(c, dict) and c.get("text"):
                r = c.get("role", "")
                out.append(html.escape((r + ": " if r else "") + c["text"]))
    return " · ".join(out)

def yld(e):
    for m in (e.get("metrics") or []):
        if isinstance(m, dict) and str(m.get("type") or m.get("role") or "").lower() == "yield":
            return html.escape(str(m.get("content") or m.get("text") or m.get("value") or "").strip())
    return ""

def rxn_row(idx, e, tag=""):
    return ('<div class="rxn"><div class="n">%s%s</div>'
            '<div class="s">%s</div>'
            '<div class="ar">&rarr;%s</div>'
            '<div class="s">%s</div>'
            '<div class="y">%s</div></div>'
            % (tag, idx, side(e.get("reactants")),
               ('<div class="c">%s</div>' % conds(e)) if conds(e) else "",
               side(e.get("products")), yld(e)))

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    v2 = json.load(open(sys.argv[1]))
    out = sys.argv[2] if len(sys.argv) > 2 else "v2_view.html"
    figb64 = None
    if len(sys.argv) > 3 and os.path.exists(sys.argv[3]):
        figb64 = base64.b64encode(open(sys.argv[3], "rb").read()).decode()

    ex = v2.get("selected_examples") or []
    tpl = v2.get("reaction_templates") or []
    fig = ('<div class="fig"><img src="data:image/png;base64,%s"></div>' % figb64) if figb64 else ""
    tpl_html = "".join(rxn_row(i + 1, t, "T") for i, t in enumerate(tpl))
    ex_html = "".join(rxn_row(i + 1, e) for i, e in enumerate(ex))
    doc = HTML % {
        "src": html.escape(os.path.basename(sys.argv[1])),
        "rclass": html.escape(str(v2.get("reaction_class") or "—")),
        "nex": len(ex), "ntpl": len(tpl), "fig": fig,
        "tpl": tpl_html or '<div class="dim">（无通式模板）</div>',
        "ex": ex_html or '<div class="dim">（无具体示例）</div>',
    }
    open(out, "w").write(doc)
    print("wrote %s  (%d examples, %d templates)" % (out, len(ex), len(tpl)))

HTML = """<!doctype html><meta charset="utf-8"><title>V2: %(src)s</title><style>
body{font:14px system-ui,'PingFang SC',sans-serif;margin:0;background:#f4f6f8;color:#1a2530}
header{background:#141b24;color:#e6edf4;padding:14px 20px}
header b{color:#5ad1c4}.meta{color:#8ea0b2;font-size:13px;margin-top:3px}
h2{font-size:15px;margin:20px 20px 8px;color:#33475b}
.fig{margin:14px 20px}.fig img{max-width:100%%;border:1px solid #d8e0e8;border-radius:8px;background:#fff}
.rxn{display:grid;grid-template-columns:40px 1fr auto 1fr 56px;gap:10px;align-items:center;
  background:#fff;border:1px solid #e2e8ee;border-radius:8px;margin:8px 20px;padding:8px}
.n{font-weight:700;color:#5ad1c4;font-family:ui-monospace,monospace;text-align:center}
.s{display:flex;flex-wrap:wrap;gap:4px;align-items:center;justify-content:center}
.mol{border:1px solid #eef2f6;border-radius:6px;background:#fff;text-align:center;padding:2px}
.mol code{display:block;font-size:9px;color:#8ea0b2;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plus{color:#8ea0b2;font-size:18px;font-weight:700;padding:0 2px}
.ar{color:#33475b;font-size:22px;text-align:center;min-width:120px}
.c{font-size:11px;color:#5a6b7a;max-width:180px;line-height:1.3}
.y{font-family:ui-monospace,monospace;font-weight:700;color:#0e9b8c;text-align:right}
.dim{color:#8ea0b2;margin:8px 20px}
</style>
<header><b>V2 提取可视化</b> — %(src)s<div class="meta">reaction_class: %(rclass)s ·
 %(nex)d 个具体示例 · %(ntpl)d 个通式模板</div></header>
%(fig)s
<h2>通式模板 reaction_templates（带 R / * 的 markush）</h2>%(tpl)s
<h2>具体示例 selected_examples</h2>%(ex)s
"""

if __name__ == "__main__":
    main()
