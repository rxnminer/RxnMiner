#!/usr/bin/env python3
"""Single reaction figure -> V2 JSON, using the chem_harness agent (gemini) + fhr R-group engine.

  # Vertex (your own service-account JSON), fhr on, vision-only (no external OCSR):
  FHR_REVERSE=1 VERTEX_CREDS=/path/to/sa.json \
      python extract.py --image examples/synfact/1.png --out-dir out --no-ocsr

  # OR simplest: your OWN Gemini API key (AIza..., Google AI Studio) -- no GCP project:
  FHR_REVERSE=1 GEMINI_API_KEY=AIza... \
      python extract.py --image examples/synfact/1.png --out-dir out --provider byo-gemini --no-ocsr

  # then visualize:
  python scripts/render_v2.py out/1.v2.json out/1.html examples/synfact/1.png
"""
import bootstrap  # noqa: F401  (sets sys.path + CHEM_REPO)
import argparse
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", required=True, help="one reaction figure (png/jpg)")
    ap.add_argument("--out-dir", required=True, help="writes <stem>.v2.json (+ transcript, plan) here")
    ap.add_argument("--provider", default="gemini-vertex", help="gemini-vertex | local-qwen | byo-gemini")
    ap.add_argument("--model", default=None,
                    help="default: the provider's own model (gemini-vertex->gemini-3.1-pro-preview, byo-gemini->gemini-2.5-pro)")
    ap.add_argument("--no-ocsr", dest="ocsr", action="store_false",
                    help="vision-only; skip the MolParser OCSR endpoint (default ON but endpoint may be unreachable)")
    a = ap.parse_args()
    from rxncaption_extract import run_one   # reuse the batch runner's single-figure worker
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    rec = run_one(Path(a.image), out, a.provider, a.model, run_ocsr=a.ocsr)
    # run_one swallows agent failures into rec["status"]; printing "done" regardless made a run that
    # produced nothing (e.g. no credentials -> status "error" in 1.6s) look successful to any caller.
    v2 = out / ("%s.v2.json" % Path(a.image).stem)
    status = (rec or {}).get("status")
    if not v2.exists() or status == "error":
        reason = (rec or {}).get("reason") or "no %s was written" % v2.name
        print("FAILED: %s" % reason, file=sys.stderr)
        return 1
    if status != "done":
        print("warning: extraction finished as '%s' (partial result)" % status, file=sys.stderr)
    print("done -> %s" % v2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
