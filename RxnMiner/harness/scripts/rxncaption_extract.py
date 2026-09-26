#!/usr/bin/env python3
"""Run our agent over a folder of figure images WITHOUT ground truth (database GENERATION), e.g. the full
RxnCaption corpus. Reuses chem_harness.run; NO scoring. Resumable: an already-produced valid <id>.v2.json is
skipped. Reuses each image's pre-computed OCSR (`<id>_molparserx.json`) if present (flattened to the molparser
format the agent reads); else runs vision-only.

Intended to run SERVER-SIDE (RxnCaption images + the free local-qwen vLLM both live on dp_server2). The qwen key
is read from the `local_qwen_key` env at runtime (inject it from the server's key file; never hard-code/print it).

  python scripts/rxncaption_extract.py --src <images_dir> --out <db_dir> --limit 500 --concurrency 5
  python scripts/rxncaption_extract.py --src <images_dir> --out <db_dir> --shard 0/4         # one of 4 parallel workers
"""
from __future__ import annotations
import argparse, json, os, shutil, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

def _find_repo(start: Path) -> Path:
    """Walk up to the dir that actually holds agent/. The release ships this file BOTH at the root and under
    scripts/, so a fixed parent.parent resolved above the repo for the root copy and the documented
    `python -m rxncaption_extract` died with ModuleNotFoundError before printing --help."""
    for cand in (start, *start.parents):
        if (cand / "agent" / "chem_harness.py").exists():
            return cand
    return start.parent.parent          # unchanged fallback


REPO = _find_repo(Path(__file__).resolve().parent)
sys.path.insert(0, str(REPO / "agent")); sys.path.insert(0, str(REPO / "scripts"))
import chem_harness as H            # noqa: E402
import reaction_check as RC         # noqa: E402

_MAN_LOCK = threading.Lock()


def _molparserx_to_molparser(mx_path: Path, out_path: Path) -> int:
    """RxnCaption `*_molparserx.json` ({source, results:[[det,...]]}) -> the flat list the agent reads
    (each det gains a bbox_index). Returns detection count, or -1 if unparseable."""
    try:
        mx = json.loads(mx_path.read_text())
        dets = []
        for group in (mx.get("results") or []):
            for d in (group or []):
                if isinstance(d, dict):
                    dets.append({**d, "bbox_index": len(dets)})
        out_path.write_text(json.dumps(dets, ensure_ascii=False))
        return len(dets)
    except Exception:
        return -1


def _find_molparserx(png: Path):
    for cand in (png.with_name(png.stem + "_molparserx.json"),
                 png.with_name(png.stem + "_molparser.json")):
        if cand.exists():
            return cand
    return None


def run_one(png: Path, out_dir: Path, provider: str, model: str, run_ocsr: bool = True):
    fid = png.stem
    out_v2 = out_dir / f"{fid}.v2.json"
    work = out_dir / "_work" / fid
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(png, work / "0.png")
    if not (out_dir / f"{fid}.png").exists():           # persist the figure for the UI regardless of emit success
        shutil.copyfile(png, out_dir / f"{fid}.png")
    ocsr_cache = out_dir / "_ocsr" / f"{fid}.json"     # persisted OCSR (resume + UI Molecules tab)
    mx = _find_molparserx(png)
    n_det = 0
    if mx:
        n_det = _molparserx_to_molparser(mx, work / "0_molparser.json")
    elif run_ocsr:
        try:
            if ocsr_cache.exists():
                dets = json.loads(ocsr_cache.read_text())
            else:
                from tools import molparser            # remote OCSR (CN IP, no SOCKS — tools.py handles it)
                dets = molparser(str(work / "0.png"))
                ocsr_cache.parent.mkdir(parents=True, exist_ok=True)
                ocsr_cache.write_text(json.dumps(dets, ensure_ascii=False))
            (work / "0_molparser.json").write_text(json.dumps(dets, ensure_ascii=False))
            n_det = len(dets)
        except Exception:
            n_det = 0
    use_det = n_det > 0
    t = time.time()
    try:
        res = H.run(str(work), 0, str(out_v2), provider=provider, model_name=model, use_detections=use_det,
                    max_tokens=int(os.environ.get("MAX_TOKENS", "2000000")),   # dense figures need room (400k default cut 816 off mid-run)
                    max_iters=int(os.environ.get("MAX_ITERS", "50")))
        valid = bool(res.get("valid"))
        v2 = json.loads(out_v2.read_text()) if out_v2.exists() else {}
        # GT-free self-check (render-and-compare) as a quality signal
        try:
            sk = RC.reaction_check(v2)
            conform = sk.get("conform_rate") if sk.get("applicable") else None
        except Exception:
            conform = None
        if out_v2.exists():
            shutil.copyfile(work / "0.png", out_dir / f"{fid}.png") if not (out_dir / f"{fid}.png").exists() else None
            (out_dir / f"{fid}.transcript.json").write_text(json.dumps(res.get("transcript") or [], ensure_ascii=False, indent=1))
        rec = {"id": fid, "status": "done" if valid else "incomplete", "valid": valid,
               "n_examples": len(v2.get("selected_examples") or []),
               "n_templates": len(v2.get("reaction_templates") or []),
               "tokens_in": res.get("tokens_in"), "tokens_out": res.get("tokens_out"),
               "tokens_cached": res.get("tokens_cached"), "iters": res.get("iters"),
               "use_detections": use_det, "n_detections": n_det if use_det else 0,
               "conform_rate": conform, "secs": round(time.time() - t, 1)}
    except Exception as e:
        rec = {"id": fid, "status": "error", "valid": False,
               "reason": f"{type(e).__name__}: {e}", "secs": round(time.time() - t, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="dir of figure images (searched recursively for *.png/*.jpg)")
    ap.add_argument("--out", required=True, help="DB output dir for <id>.v2.json + _run_manifest.json")
    ap.add_argument("--provider", default="local-qwen")
    ap.add_argument("--model", default="Qwen3.5-397B-A17B-FP8")
    ap.add_argument("--limit", type=int, default=0, help="cap number of NEW figures this run (0 = all; pilot=500)")
    ap.add_argument("--shard", default="0/1", help="i/n : process only images whose sorted index %% n == i")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--no-ocsr", dest="ocsr", action="store_false", help="vision-only (skip MolParser OCSR)")
    ap.set_defaults(ocsr=True)
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    man_path = out / "_run_manifest.json"
    man = json.loads(man_path.read_text()) if man_path.exists() else {"runs": {}}
    runs = man["runs"]

    imgs = sorted(p for p in src.rglob("*")
                  if p.suffix.lower() in (".png", ".jpg", ".jpeg")
                  and not p.name.startswith("._") and "__MACOSX" not in p.parts)
    si, sn = (int(x) for x in a.shard.split("/"))
    if sn > 1:
        imgs = [p for idx, p in enumerate(imgs) if idx % sn == si]
    # resume: skip ids already done with a valid V2 on disk
    todo = [p for p in imgs if not (runs.get(p.stem, {}).get("valid") and (out / f"{p.stem}.v2.json").exists())]
    if a.limit:
        todo = todo[:a.limit]
    print(f"[{time.strftime('%H:%M:%S')}] src={src} total={len(imgs)} todo={len(todo)} "
          f"(provider={a.provider} model={a.model} conc={a.concurrency} shard={a.shard})", flush=True)

    done = err = 0
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(run_one, p, out, a.provider, a.model, a.ocsr): p for p in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            rec = fut.result()
            with _MAN_LOCK:
                runs[rec["id"]] = rec
                man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2))
            ok = rec.get("valid")
            done += int(bool(ok)); err += int(rec.get("status") == "error")
            print(f"[{time.strftime('%H:%M:%S')}] {i}/{len(todo)} {rec['id']:36} "
                  f"{rec['status']:10} ex={rec.get('n_examples')} tok={rec.get('tokens_in')}/{rec.get('tokens_out')} "
                  f"conform={rec.get('conform_rate')} {rec.get('secs')}s", flush=True)

    total_valid = sum(1 for v in runs.values() if v.get("valid"))
    print(f"\n[{time.strftime('%H:%M:%S')}] this pass: done={done} err={err} of {len(todo)}; "
          f"DB total valid={total_valid}/{len(runs)} attempted", flush=True)


if __name__ == "__main__":
    main()
