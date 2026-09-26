#!/usr/bin/env python3
"""Import release agent output (single version) into chem_reviewer's PDF/<slug>/ layout.

Unlike import_bal300_to_reviewer.py (which loads two versions per figure), the release
produces ONE extraction per figure: <fid>.v2.json (+ .plan.json + .transcript.json). This
adapter drops each into PDF/rel__<fid>/ so the EXISTING chem_reviewer (Ketcher editor)
renders and edits it — no standalone HTML.

The reviewer draws a row's yield from a `conditions` entry with role='yield' (not from
`metrics`), so we fold each example's metrics into an extra conditions step for display,
keeping the original `metrics` intact. The plan + transcript are copied alongside so the
reviewer dir carries the full provenance (three human-readable docs).

  python scripts/import_release_to_reviewer.py --out-dir <release/output> \
      --images <images dir> --pdf-root <viewer/PDF> [--limit N]
"""
import argparse, json, shutil, glob, os, copy


def _fold_metrics_into_conditions(v2):
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
            e["conditions"] = conds + [step]
    return v2


def _prep(d, fid, slug, extractor):
    d = copy.deepcopy(d)
    d = _fold_metrics_into_conditions(d)
    d["task_id"] = fid
    d["paper_id"] = slug
    d["anchor_image"] = "1.png"
    d["anchor_figure"] = fid.replace("-with-examples", "")
    d["anchor_page"] = 1
    d["extractor"] = extractor
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True, help="release/output with <fid>.v2.json")
    ap.add_argument("--images", required=True)
    ap.add_argument("--pdf-root", required=True, help="viewer/PDF root the reviewer serves")
    ap.add_argument("--prev-dir", default="", help="optional earlier extraction dir -> task 2 (compare)")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.pdf_root, exist_ok=True)
    n = 0
    for jp in sorted(glob.glob(os.path.join(a.out_dir, "*.v2.json"))):
        if jp.endswith(".plan.json"):
            continue
        fid = os.path.basename(jp)[:-len(".v2.json")]
        img = os.path.join(a.images, fid + ".png")
        if not os.path.exists(img):
            img = os.path.join(a.out_dir, fid + ".png")
        if not os.path.exists(img):
            print("  skip %s (no image)" % fid)
            continue
        slug = "rel__" + fid
        sd = os.path.join(a.pdf_root, slug)
        os.makedirs(sd, exist_ok=True)
        v1 = _prep(json.load(open(jp)), fid, slug, "release · gemini-3.1-pro + fhr (FIXED engine)")
        shutil.copy(img, os.path.join(sd, "1.png"))
        json.dump(v1, open(os.path.join(sd, "1_predicted_output.json"), "w"), ensure_ascii=False, indent=1)
        # carry the other two docs for provenance (viewer ignores them, humans can read them)
        for suf, dst in ((".v2.json.plan.json", "1_plan.json"), (".transcript.json", "1_transcript.json")):
            src = os.path.join(a.out_dir, fid + suf)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(sd, dst))
        figs = [{"id": 1, "filename": "1.png", "anchor_figure": "task1 · FIXED engine", "page": 1,
                 "n_examples": len(v1.get("selected_examples") or [])}]
        # optional task 2 = earlier extraction, for before/after comparison
        prevp = os.path.join(a.prev_dir, fid + ".v2.json") if a.prev_dir else ""
        if prevp and os.path.exists(prevp):
            v2 = _prep(json.load(open(prevp)), fid, slug, "release · earlier extraction (compare)")
            shutil.copy(img, os.path.join(sd, "2.png"))
            json.dump(v2, open(os.path.join(sd, "2_predicted_output.json"), "w"), ensure_ascii=False, indent=1)
            figs.append({"id": 2, "filename": "2.png", "anchor_figure": "task2 · earlier (compare)", "page": 1,
                         "n_examples": len(v2.get("selected_examples") or [])})
        reactions = {
            "paper_id": slug, "doi": None, "journal": "Synfacts", "year": None,
            "title": fid.replace("-with-examples", ""), "page_count": 1,
            "extractor": "gemini-3.1-pro + fhr (release)",
            "figures": figs,
        }
        json.dump(reactions, open(os.path.join(sd, "reactions.json"), "w"), ensure_ascii=False, indent=1)
        n += 1
        print("  %s  (%d ex)" % (slug, len(v1.get("selected_examples") or [])))
        if a.limit and n >= a.limit:
            break
    print("imported %d figures -> %s" % (n, a.pdf_root))


if __name__ == "__main__":
    main()
