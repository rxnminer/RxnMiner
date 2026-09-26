#!/usr/bin/env python3
"""Import balanced-300 into chem_reviewer's PDF/<slug>/ format, loading BOTH versions per figure:
  task 1 = version 1 (pure gemini+fhr agent)
  task 2 = version 2 (GT + agent hybrid, the annotation start)

Each slug PDF/bal300__<id>/ gets 1.png,1_predicted_output.json,2.png,2_predicted_output.json,reactions.json.

The chem_reviewer renders a row's yield from a `conditions` entry with role='yield' (not from `metrics`),
so we fold each example's metrics (yield/dr/ee/...) into an extra conditions step for display, keeping the
original `metrics` field intact.

  python scripts/import_bal300_to_reviewer.py --v1-dir <agent v2.json dir> --v2-dir <annotation dir> \
      --images <images dir> --out <PDF root> [--limit N]
"""
import argparse, json, shutil, glob, os, copy


def _fold_metrics_into_conditions(v2):
    """Append a conditions step carrying each example's metrics as role-tagged entries so the viewer draws them."""
    for e in (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or []):
        mets = e.get("metrics") or []
        if not mets:
            continue
        step = []
        for m in mets:
            if not isinstance(m, dict):
                continue
            role = (m.get("type") or m.get("role") or "yield")
            text = str(m.get("content") or m.get("text") or m.get("value") or "").strip()
            if text:
                step.append({"role": role, "text": text})
        if step:
            conds = e.get("conditions")
            if not isinstance(conds, list):
                conds = []
            conds = conds + [step]
            e["conditions"] = conds
    return v2


def _prep(d, fid, slug, tid, extractor):
    d = copy.deepcopy(d)
    d = _fold_metrics_into_conditions(d)
    d["task_id"] = fid
    d["paper_id"] = slug
    d["anchor_image"] = f"{tid}.png"
    d["anchor_figure"] = fid.replace("-with-examples", "")
    d["anchor_page"] = 1
    d["extractor"] = extractor
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1-dir", required=True)
    ap.add_argument("--v2-dir", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    n = 0
    for jp in sorted(glob.glob(os.path.join(a.v2_dir, "*.json"))):
        fid = os.path.basename(jp)[:-len(".json")]
        if fid.startswith("_"):
            continue
        img = os.path.join(a.images, fid + ".png")
        v1p = os.path.join(a.v1_dir, fid + ".v2.json")
        if not (os.path.exists(img) and os.path.exists(v1p)):
            continue
        slug = "bal300__" + fid
        sd = os.path.join(a.out, slug)
        os.makedirs(sd, exist_ok=True)
        v1 = _prep(json.load(open(v1p)), fid, slug, "1", "V1 · gemini-3.1-pro + fhr (agent)")
        v2 = _prep(json.load(open(jp)), fid, slug, "2", "V2 · GT + agent hybrid (annotate here)")
        notes = v2.get("_annotation_notes") or {}
        shutil.copy(img, os.path.join(sd, "1.png"))
        shutil.copy(img, os.path.join(sd, "2.png"))
        json.dump(v1, open(os.path.join(sd, "1_predicted_output.json"), "w"), ensure_ascii=False, indent=1)
        json.dump(v2, open(os.path.join(sd, "2_predicted_output.json"), "w"), ensure_ascii=False, indent=1)
        reactions = {
            "paper_id": slug, "doi": None, "journal": "Synfacts", "year": None,
            "title": fid.replace("-with-examples", ""), "page_count": 1,
            "extractor": "gemini-3.1-pro + fhr",
            "figures": [
                {"id": 1, "filename": "1.png", "anchor_figure": "V1 · agent", "page": 1,
                 "n_examples": len(v1.get("selected_examples") or [])},
                {"id": 2, "filename": "2.png", "anchor_figure": "V2 · GT+agent (annotate)", "page": 1,
                 "n_examples": len(v2.get("selected_examples") or []), "annotation_notes": notes},
            ],
        }
        json.dump(reactions, open(os.path.join(sd, "reactions.json"), "w"), ensure_ascii=False, indent=1)
        n += 1
        if a.limit and n >= a.limit:
            break
    print("imported %d figures (v1+v2 each) -> %s" % (n, a.out))


if __name__ == "__main__":
    main()
