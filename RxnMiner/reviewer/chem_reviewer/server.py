#!/usr/bin/env python3
"""chem_reviewer — v2-schema chemistry reaction reviewer (FastAPI backend).

Serves the v2 dataset under ``PDF/`` (one paper per subdirectory, one
``N_predicted_output.json`` per anchor figure). The original files are
NEVER mutated — edits land in sibling sidecars:

    PDF/<slug>/<N>_predicted_output.reviewed.json          ← edited copy
    PDF/<slug>/<N>_predicted_output.reviewed-vN.json       ← immutable versions
    PDF/<slug>/<N>_predicted_output.reviewed.diff.json     ← RFC-6902-ish ops
    PDF/<slug>/<N>_predicted_output.reviewed.diff.md       ← chemist-readable
    PDF/<slug>/<N>_predicted_output.reviewed.multistep.json ← cytoscape positions

Usage:
    PDF_ROOT=/path/to/reviewer-data python reviewer/chem_reviewer/server.py --port 8767
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Local helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diff_utils import compute_json_diff, compute_markdown_diff  # noqa: E402
from v2_loader import (  # noqa: E402
    DIFF_JSON_SUFFIX,
    DIFF_MD_SUFFIX,
    SIDECAR_SUFFIX,
    collect_molparser_detections,
    deep_merge_preserving,
    diff_json_path,
    diff_md_path,
    extract_task_meta,
    list_predicted_files,
    load_task,
    merge_raw_molparser_detections,
    paper_meta_path,
    pdf_path_for_paper,
    predicted_path,
    reactions_overview_path,
    reviewed_path,
    task_id_from_filename,
)
from pdf_render import DEFAULT_DPI, get_or_render_page, page_count  # noqa: E402

# ── Paths ─────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent                     # reviewer/chem_reviewer/
REVIEWER_ROOT = HERE.parent                                # reviewer/
PROJECT_ROOT = REVIEWER_ROOT.parent                        # RxnMiner/
def _cli_value(flag: str) -> str | None:
    """Read an early path option before StaticFiles mounts are constructed."""
    try:
        index = sys.argv.index(flag)
        return sys.argv[index + 1]
    except (ValueError, IndexError):
        return None


_pdf_env = os.environ.get("PDF_ROOT") or _cli_value("--data-root")
DATA_ROOT = Path(_pdf_env).expanduser().resolve() if _pdf_env else REVIEWER_ROOT / "PDF"
KETCHER_DIR = Path(os.environ.get("KETCHER_DIR") or (REVIEWER_ROOT / "vendor" / "ketcher"))
WEB_DIR = HERE / "web"
# Stage-file source: optional harness extraction work_dirs. Keep this integration explicit while
# reviewer code/data remain independent from the harness package.
WORK_FIGURE_ROOT = Path(os.environ.get("WORK_FIGURE_ROOT") or
                        (PROJECT_ROOT / "harness" / "_external" /
                         "benchmark_opencode" / "work_figure"))

if not DATA_ROOT.exists():
    print(
        f"[WARN] data dir not present yet: {DATA_ROOT}\n"
        "  Pass --data-root or PDF_ROOT; launchers may create the external store before startup.",
        file=sys.stderr,
    )


def _safe_segment(s: str) -> str:
    if not s or "/" in s or ".." in s or "\0" in s:
        raise HTTPException(status_code=400, detail=f"bad path segment: {s!r}")
    return s


def _paper_dir(slug: str) -> Path:
    """``PDF/<slug>`` with safety + existence check."""
    slug = _safe_segment(slug)
    d = DATA_ROOT / slug
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"paper dir not found: {slug}")
    return d


# ── App ───────────────────────────────────────────────────────────────
app = FastAPI(title="chem_reviewer (v2)", version="0.3")


def _configured_values(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return defaults
    return tuple(item.strip() for item in raw.split(",") if item.strip())


TRUSTED_ORIGINS = _configured_values(
    "CHEMRXN_REVIEWER_TRUSTED_ORIGINS",
    (
        "http://127.0.0.1:8767",
        "http://localhost:8767",
        "http://127.0.0.1:8785",
        "http://localhost:8785",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
    ),
)
_DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")
if os.environ.get("PYTEST_CURRENT_TEST"):
    _DEFAULT_ALLOWED_HOSTS += ("testserver",)
ALLOWED_HOSTS = frozenset(
    host.lower()
    for host in _configured_values(
        "CHEMRXN_REVIEWER_ALLOWED_HOSTS",
        _DEFAULT_ALLOWED_HOSTS,
    )
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(TRUSTED_ORIGINS),
    allow_methods=["GET", "HEAD", "OPTIONS", "POST", "DELETE"],
    allow_headers=["Accept", "Content-Type"],
)


def _request_hostname(host_header: str) -> str | None:
    try:
        return urllib.parse.urlsplit(f"//{host_header}").hostname
    except ValueError:
        return None


@app.middleware("http")
async def _protect_local_reviewer_boundary(request: Request, call_next):
    """Keep the local reviewer private from DNS rebinding and cross-site browser requests.

    The harness and reviewer intentionally use separate loopback ports today, so their exact
    launcher origins are allowlisted instead of enabling wildcard CORS.
    """
    host_header = request.headers.get("host", "")
    hostname = _request_hostname(host_header)
    if not hostname or hostname.lower() not in ALLOWED_HOSTS:
        return JSONResponse(status_code=403, content={"detail": "Untrusted Host"})

    origin = request.headers.get("origin")
    if origin:
        try:
            parsed_origin = urllib.parse.urlsplit(origin)
            normalized_origin = (
                f"{parsed_origin.scheme.lower()}://{parsed_origin.netloc.lower()}"
            )
        except ValueError:
            normalized_origin = ""
        request_origin = f"{request.url.scheme.lower()}://{host_header.lower()}"
        if normalized_origin not in TRUSTED_ORIGINS and normalized_origin != request_origin:
            return JSONResponse(status_code=403, content={"detail": "Cross-site Origin rejected"})
    elif request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return JSONResponse(status_code=403, content={"detail": "Cross-site request rejected"})

    response = await call_next(request)
    frame_ancestors = " ".join(("'self'", *TRUSTED_ORIGINS))
    response.headers["Content-Security-Policy"] = f"frame-ancestors {frame_ancestors}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.middleware("http")
async def _no_cache(request, call_next):
    # The SPA modules + the bbox/task JSON have no version hash, so browsers cache them and
    # keep showing stale extractions/box counts after a re-import. Force revalidation so a
    # normal reload always reflects the latest data (no manual hard-refresh needed).
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# ── Static mounts ─────────────────────────────────────────────────────
if KETCHER_DIR.exists():
    app.mount(
        "/ketcher",
        StaticFiles(directory=str(KETCHER_DIR), html=True),
        name="ketcher",
    )
else:
    print(
        f"[WARN] ketcher dir missing: {KETCHER_DIR}\n"
        f"  Ketcher modal will be unavailable. (Expected if you haven't "
        f"populated ChemReaction_benchmark-main/viewer/dist/ketcher/)",
        file=sys.stderr,
    )

# `/static` serves the reviewer's own assets (web/index.html etc.)
app.mount("/static", StaticFiles(directory=str(WEB_DIR), html=True), name="static")
# Expose PDF/ tree so raw_pred.json / raw_gt.json can be fetched directly by compare.html
app.mount("/data", StaticFiles(directory=str(DATA_ROOT)), name="data")


# ── Root ──────────────────────────────────────────────────────────────


@app.get("/")
def root():
    return RedirectResponse(url="/static/")


@app.get("/compare/{slug}/{tid}")
def compare_page(slug: str, tid: str):
    """Side-by-side pred (right) vs GT (left) view — for ChemEAGLE benchmark figs.

    Independent of the Vue SPA. Self-contained HTML that loads RDKit.js,
    fetches /api/paper/{slug}/task/{tid} (which gives both predicted_output
    and reviewed), and renders two synchronised columns of reaction cards.
    """
    return RedirectResponse(url=f"/static/compare.html?slug={slug}&tid={tid}")


# ── Paper-level endpoints ─────────────────────────────────────────────


@app.get("/api/papers")
def list_papers():
    """List all PDF/<slug>/ paper directories that hold v2 extractions.

    Returns: ``[{slug, title, doi, journal, page_count, n_tasks,
    n_reviewed, n_multistep, n_failed_total, last_reviewed}]``.
    """
    items = []
    for d in sorted(DATA_ROOT.iterdir()):
        if not d.is_dir():
            continue
        figs = list_predicted_files(d)
        if not figs:
            continue

        # Paper-level overview (optional)
        rxn_fp = reactions_overview_path(d)
        meta: dict = {}
        if rxn_fp.exists():
            try:
                meta = json.loads(rxn_fp.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[WARN] bad reactions.json {rxn_fp}: {e}", file=sys.stderr)

        # Reviewed counter + multistep / failed aggregates (cheap scan)
        n_reviewed = 0
        n_multistep = 0
        n_failed_total = 0
        last_reviewed: float | None = None
        for fig_fp in figs:
            tid = task_id_from_filename(fig_fp.name)
            assert tid is not None
            rev_fp = reviewed_path(d, tid)
            if rev_fp.exists():
                n_reviewed += 1
                t = rev_fp.stat().st_mtime
                if last_reviewed is None or t > last_reviewed:
                    last_reviewed = t
            try:
                data = json.loads(fig_fp.read_text(encoding="utf-8"))
                if data.get("is_multistep_synthesis"):
                    n_multistep += 1
                for ex in (data.get("selected_examples") or []):
                    if (ex.get("_outcome") or {}).get("is_failed"):
                        n_failed_total += 1
                for tpl in (data.get("reaction_templates") or []):
                    if (tpl.get("_outcome") or {}).get("is_failed"):
                        n_failed_total += 1
            except Exception:
                pass

        items.append({
            "slug": d.name,
            "title": meta.get("title", d.name),
            "doi": meta.get("doi"),
            "journal": meta.get("journal"),
            "year": meta.get("year"),
            "page_count": meta.get("page_count", 0),
            "n_tasks": len(figs),
            "n_reviewed": n_reviewed,
            "n_multistep": n_multistep,
            "n_failed": n_failed_total,
            "last_reviewed": (
                datetime.fromtimestamp(last_reviewed, timezone.utc).isoformat()
                if last_reviewed
                else None
            ),
        })
    return {"papers": items}


@app.get("/api/paper/{slug}/manifest")
def paper_manifest(slug: str):
    """Synthesize a paper_manifest from PDF/<slug>/*_predicted_output.json.

    PDF/ has no `paper_manifest.json`; we build it on the fly so the SPA
    sees a familiar shape (`tasks` dict keyed by task_id).
    """
    d = _paper_dir(slug)
    figs = list_predicted_files(d)
    if not figs:
        raise HTTPException(status_code=404, detail=f"no figures in {slug}")

    # Optional paper-level overview
    rxn_fp = reactions_overview_path(d)
    paper_meta = {}
    if rxn_fp.exists():
        try:
            paper_meta = json.loads(rxn_fp.read_text(encoding="utf-8"))
        except Exception:
            pass

    # PDF page count for the integer-indexed page renderer
    pdf_fp = pdf_path_for_paper(DATA_ROOT, slug)
    pcount = paper_meta.get("page_count") or page_count(pdf_fp)

    tasks: dict[str, dict] = {}
    page_to_tasks: dict[int, list[str]] = {}
    for fig_fp in figs:
        tid = task_id_from_filename(fig_fp.name)
        if tid is None:
            continue
        try:
            data = json.loads(fig_fp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] bad {fig_fp}: {e}", file=sys.stderr)
            continue
        meta = extract_task_meta(data, tid)

        # Reviewed status
        rev_fp = reviewed_path(d, tid)
        meta["is_reviewed"] = False
        meta["reviewed_at"] = None
        meta["review_pointer_corrupt"] = False
        meta["is_dropped"] = False
        if rev_fp.exists():
            try:
                rd = json.loads(rev_fp.read_text(encoding="utf-8"))
                meta["is_reviewed"] = True
                meta["reviewed_at"] = datetime.fromtimestamp(
                    rev_fp.stat().st_mtime,
                    timezone.utc,
                ).isoformat()
                if isinstance(rd, dict) and rd.get("_dropped"):
                    meta["is_dropped"] = True
            except Exception:
                meta["review_pointer_corrupt"] = True

        # Anchor PNG availability
        anchor_png = d / meta.get("anchor_image", f"{tid}.png")
        meta["has_anchor_png"] = anchor_png.exists()

        tasks[tid] = meta
        ap = meta.get("anchor_page")
        if isinstance(ap, int):
            page_to_tasks.setdefault(ap, []).append(tid)

    pages = [
        {"page_index": pi, "task_ids": sorted(tids, key=lambda x: int(x))}
        for pi, tids in sorted(page_to_tasks.items())
    ]

    return {
        "paper_slug": slug,
        "title": paper_meta.get("title", slug),
        "doi": paper_meta.get("doi"),
        "journal": paper_meta.get("journal"),
        "year": paper_meta.get("year"),
        "extractor": paper_meta.get("extractor", ""),
        "page_count": pcount,
        "pdf_filename": f"{slug}.pdf" if pdf_fp.exists() else None,
        "pages": pages,
        "tasks": tasks,
    }


# ── Anchor / page image endpoints ─────────────────────────────────────


@app.get("/api/paper/{slug}/anchor/{tid}")
def paper_anchor(slug: str, tid: str):
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    # Resolve anchor PNG via N_predicted_output.json's anchor_image field
    # (typically just "<tid>.png" but could differ)
    fig_fp = predicted_path(d, tid)
    fname = f"{tid}.png"
    if fig_fp.exists():
        try:
            data = json.loads(fig_fp.read_text(encoding="utf-8"))
            fname = data.get("anchor_image") or fname
        except Exception:
            pass
    fp = d / fname
    if not fp.exists():
        raise HTTPException(status_code=404, detail=f"no anchor: {slug}/{tid}")
    return FileResponse(fp, media_type="image/png")


@app.get("/api/paper/{slug}/page/{n}")
def paper_page(slug: str, n: int):
    """On-demand render PDF page n (1-indexed). Caches to PDF/<slug>/.cache/."""
    d = _paper_dir(slug)
    pdf_fp = pdf_path_for_paper(DATA_ROOT, slug)
    cached = get_or_render_page(d, pdf_fp, n, dpi=DEFAULT_DPI)
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail=f"page {n} render failed (PDF missing or page out of range)",
        )
    return FileResponse(cached, media_type="image/png")


@app.get("/api/paper/{slug}/pdf")
def paper_pdf(slug: str):
    _safe_segment(slug)
    pdf_fp = pdf_path_for_paper(DATA_ROOT, slug)
    if not pdf_fp.exists():
        raise HTTPException(status_code=404, detail=f"no source PDF for {slug}")
    return FileResponse(pdf_fp, media_type="application/pdf")


# ── molparser detection endpoint ──────────────────────────────────────


@app.get("/api/paper/{slug}/molparser/{tid}")
def paper_molparser(slug: str, tid: str):
    """Return raw MolParser evidence when imported, else bbox-only V2 fallback.

    The fallback deliberately carries ``raw_available=false`` because its SMILES
    comes from final V2 molecules, not the original MolParser response.
    """
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    fig_fp = predicted_path(d, tid)
    if not fig_fp.exists():
        return JSONResponse([])
    try:
        data = json.loads(fig_fp.read_text(encoding="utf-8"))
    except Exception:
        return JSONResponse([])

    raw_fp = d / f"{tid}_molparser.json"
    if raw_fp.exists():
        try:
            raw = json.loads(raw_fp.read_text(encoding="utf-8"))
            merged = merge_raw_molparser_detections(raw, data)
            if merged:
                return JSONResponse(merged)
        except Exception:
            pass

    fallback = collect_molparser_detections(data)
    for item in fallback:
        item["raw_available"] = False
    return JSONResponse(fallback)


# ── Task read endpoint ────────────────────────────────────────────────


@app.get("/api/paper/{slug}/task/{tid}")
def paper_task(slug: str, tid: str):
    """Return a v2 task. ``data`` is reviewed-overlay if present, else original.

    ``original_data`` is always the pristine on-disk file.
    """
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    displayed, original, is_reviewed = load_task(d, tid)
    if displayed is None:
        raise HTTPException(
            status_code=404,
            detail=f"no {tid}_predicted_output.json in {slug}",
        )
    rev_fp = reviewed_path(d, tid)
    reviewed_meta = (
        displayed.get("_reviewed_meta", {})
        if is_reviewed and isinstance(displayed, dict) else {}
    )
    reviewed_at = None
    if is_reviewed and rev_fp.exists():
        reviewed_at = (
            reviewed_meta.get("reviewed_at")
            or datetime.fromtimestamp(
                rev_fp.stat().st_mtime,
                timezone.utc,
            ).isoformat()
        )
    review_head = _review_head(d, tid, strict=False)

    task_meta = extract_task_meta(original, tid)

    return {
        "slug": slug,
        "task_id": tid,
        "data": displayed,
        "original_data": original,
        "is_reviewed": is_reviewed,
        "reviewed_at": reviewed_at,
        "reviewed_version": reviewed_meta.get("version"),
        "reviewed_versions": _reviewed_versions(d, tid),
        "review_pointer_corrupt": review_head["corrupt"],
        "task_meta": task_meta,
    }


# ── GT-bug flag endpoints (compare page only) ────────────────────────
#
# Used by reviewer/chem_reviewer/web/compare.html to let a human reviewer
# mark figs whose ChemEAGLE GT is itself wrong (arrow reversed, mis-labeled
# product, etc). Persists as PDF/<slug>/gt_bug_flags.json so downstream
# F1 reporting can exclude them. The note field is free-form (e.g.
# "arrow reversed", "product 3a drawn as alcohol but should be ketone").


@app.get("/api/gt_bug_flag/{slug}/{tid}")
def gt_bug_flag_get(slug: str, tid: str):
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    fp = d / "gt_bug_flags.json"
    if not fp.exists():
        return {"flagged": False}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {"flagged": False}
    entry = (data or {}).get(tid)
    if not entry:
        return {"flagged": False}
    return {"flagged": True, "note": entry.get("note", ""), "flagged_at": entry.get("flagged_at")}


class GtBugFlagReq(BaseModel):
    note: str = ""


@app.post("/api/gt_bug_flag/{slug}/{tid}")
def gt_bug_flag_set(slug: str, tid: str, req: GtBugFlagReq):
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    fp = d / "gt_bug_flags.json"
    data = {}
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[tid] = {
        "note": req.note or "(no note)",
        "flagged_at": datetime.now(timezone.utc).isoformat(),
    }
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "flagged": True, "note": data[tid]["note"]}


@app.delete("/api/gt_bug_flag/{slug}/{tid}")
def gt_bug_flag_clear(slug: str, tid: str):
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    fp = d / "gt_bug_flags.json"
    if not fp.exists():
        return {"ok": True, "flagged": False}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.pop(tid, None)
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "flagged": False}


# ── Stage-file endpoints (per-stage extraction visualization) ────────
#
# Returns the agent's intermediate stage files for one fig so users can
# audit each phase of the extraction without leaving the compare page.
# Looks in two places (in order):
#   1. <paper_dir>/_stages/  (when stage files were pushed alongside pred)
#   2. <WORK_FIGURE_ROOT>/<slug>(.png)?/  (the original work_dir on this box)
#
# Slug-to-work_dir resolution handles both gt1 (slug WITH .png suffix in
# work_dir, slug WITHOUT .png in viewer) and gt2 (slug ends WITH .png in
# both). Stage files: molparser.json, compounds.json, template.json,
# examples.json, graph.json, outcome.json, reactions_buffer.json,
# substitution_log.json, r_extraction_log.json, prediction.json,
# fig_meta.json.

_STAGE_FILES = [
    "molparser.json",
    "compounds.json",
    "template.json",
    "examples.json",
    "graph.json",
    "outcome.json",
    "reactions_buffer.json",
    "substitution_log.json",
    "r_extraction_log.json",
    "fig_meta.json",
    "prediction.json",
]


def _resolve_work_dir(slug: str) -> Path | None:
    """Find the agent's work_dir for slug. Tries `<slug>` and `<slug>.png`."""
    if not WORK_FIGURE_ROOT.exists():
        return None
    for candidate in (slug, f"{slug}.png"):
        wd = WORK_FIGURE_ROOT / candidate
        if wd.is_dir():
            return wd
    return None


@app.get("/api/paper/{slug}/task/{tid}/stages")
def get_stages(slug: str, tid: str):
    """Return all available stage files for a fig as a dict.

    Each stage file is parsed JSON or null (if missing). Also returns
    `_source` indicating where the files came from (paper_dir/_stages vs.
    work_figure) so the UI can show "live work_dir" vs "frozen snapshot".
    """
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    out: dict = {"_source": None, "stages": {}}

    paper_stages = d / "_stages"
    work_dir = _resolve_work_dir(slug)
    src_dir: Path | None = None
    if paper_stages.is_dir():
        src_dir = paper_stages
        out["_source"] = "paper_dir/_stages"
    elif work_dir is not None:
        src_dir = work_dir
        out["_source"] = f"work_figure/{work_dir.name}"

    if src_dir is None:
        return {"_source": None, "stages": {fn: None for fn in _STAGE_FILES},
                "error": "no stage files found (neither paper_dir/_stages nor work_figure/)"}

    for fn in _STAGE_FILES:
        fp = src_dir / fn
        if not fp.exists() or fp.stat().st_size == 0:
            out["stages"][fn] = None
            continue
        try:
            out["stages"][fn] = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            out["stages"][fn] = {"_parse_error": str(e)}
    return out


# ── Stage-file edit endpoint (POST) — write back + re-run finalize ────
#
# Allows the compare page to surface every stage's content as an editable
# textarea, then push changes back to the agent's work_dir. After write,
# we automatically re-run `chem finalize` so prediction.json reflects the
# edit, and copy the regenerated prediction.json back to the viewer's
# paper_dir so the next page reload shows the corrected view.
#
# Whitelist of editable stage files — molparser.json and fig_meta.json
# are NOT editable (the first is ground truth, the second is set by the
# driver). prediction.json is REGENERATED by finalize; editing it directly
# would just get overwritten.
_EDITABLE_STAGES = {
    "compounds.json",
    "template.json",
    "examples.json",
    "graph.json",
    "outcome.json",
    "reactions_buffer.json",
}


class StageEditReq(BaseModel):
    content: dict


@app.post("/api/paper/{slug}/task/{tid}/stages/{filename}")
def edit_stage(slug: str, tid: str, filename: str, req: StageEditReq):
    """Write `content` to `<work_dir>/<filename>` then re-run finalize.

    Returns: {ok, finalize_result, prediction_summary} or {error}.
    """
    _ = _paper_dir(slug)  # 404 if paper slug invalid
    _ = _safe_segment(tid)
    if filename not in _EDITABLE_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"file not in editable whitelist: {filename}; "
                   f"editable are: {sorted(_EDITABLE_STAGES)}",
        )

    work_dir = _resolve_work_dir(slug)
    if work_dir is None:
        raise HTTPException(
            status_code=404,
            detail=f"no work_dir found for slug={slug}; agent never ran "
                   "on this fig OR work_figure/ was cleaned up",
        )

    target = work_dir / filename
    try:
        target.write_text(
            json.dumps(req.content, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"write failed: {e}")

    # Re-run chem finalize. Failure is non-fatal — we still wrote the file
    # and report the error so user can fix and retry.
    import subprocess
    repo_root = REPO_ROOT.parent
    chem_bin = repo_root / "_external/benchmark_opencode/bin/chem"
    finalize_result: dict = {"ok": False}
    if chem_bin.exists():
        try:
            proc = subprocess.run(
                [str(chem_bin), "finalize", str(work_dir)],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0:
                try:
                    finalize_result = json.loads(proc.stdout)
                    finalize_result["ok"] = True
                except json.JSONDecodeError:
                    finalize_result = {"ok": True, "stdout": proc.stdout[:500]}
            else:
                finalize_result = {
                    "ok": False,
                    "error": "finalize returned non-zero",
                    "stdout": proc.stdout[:500],
                    "stderr": proc.stderr[:500],
                }
        except subprocess.TimeoutExpired:
            finalize_result = {"ok": False, "error": "finalize timed out (>60s)"}
        except Exception as e:
            finalize_result = {"ok": False, "error": f"finalize subprocess failed: {e}"}
    else:
        finalize_result = {"ok": False, "error": f"chem binary not found: {chem_bin}"}

    # If finalize succeeded, push the regenerated prediction.json into the
    # paper_dir's PRED slot so the next compare-page load shows the corrected
    # view.
    #
    # CRITICAL: only overwrite `1_predicted_output.json` (the PRED slot).
    # NEVER overwrite `1_predicted_output.reviewed.json` — that's GT (written
    # by chem_viewer/import_gt1.py) and clobbering it makes the compare page
    # show pred-vs-pred (looking like a perfect match), which is a GT-leak-
    # adjacent display bug. Discovered 2026-05-28 on gt1_098 when both
    # columns showed "Int_I/Int_II" agent-invented names identically.
    if finalize_result.get("ok"):
        prediction_fp = work_dir / "prediction.json"
        if prediction_fp.exists():
            paper_dir = _paper_dir(slug)
            try:
                (paper_dir / "1_predicted_output.json").write_text(
                    prediction_fp.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            except Exception as e:
                finalize_result["copy_warning"] = f"saved file + finalized but copy to paper_dir failed: {e}"

    return {
        "ok": True,
        "filename": filename,
        "bytes_written": target.stat().st_size,
        "finalize": finalize_result,
    }


# ── Save endpoint (deep-merge to preserve v2 fields) ──────────────────


class SaveRequest(BaseModel):
    data: dict


class ApplyRequest(SaveRequest):
    proposal_id: str
    agent_proposal_id: Optional[str] = None


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _review_head(d: Path, tid: str, *, strict: bool = True) -> dict:
    fp = reviewed_path(d, tid)
    if not fp.exists():
        return {
            "version": 0,
            "parent_version": None,
            "parent_legacy_sha256": None,
            "sha256": None,
            "payload": None,
            "raw_bytes": None,
            "legacy": False,
            "corrupt": False,
        }
    raw = b""
    try:
        raw = fp.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        if strict:
            raise HTTPException(409, "current reviewed pointer is unreadable") from exc
        return {
            "version": None,
            "parent_version": None,
            "parent_legacy_sha256": None,
            "sha256": None,
            "payload": None,
            "raw_bytes": raw or None,
            "legacy": False,
            "corrupt": True,
        }
    meta = payload.get("_reviewed_meta", {}) if isinstance(payload, dict) else {}
    version = meta.get("version")
    if not isinstance(version, int) or version < 1:
        # Older reviewer releases wrote only the mutable compatibility pointer.
        # Keep those tasks readable and bind any new proposal to its exact
        # bytes, but do not pretend the legacy pointer is an immutable version.
        return {
            "version": 0,
            "parent_version": None,
            "parent_legacy_sha256": None,
            "sha256": _sha256_bytes(raw),
            "payload": payload,
            "raw_bytes": raw,
            "legacy": True,
            "corrupt": False,
        }
    return {
        "version": version,
        "parent_version": meta.get("parent_version"),
        "parent_legacy_sha256": meta.get("parent_legacy_sha256"),
        "sha256": _sha256_bytes(raw),
        "payload": payload,
        "raw_bytes": raw,
        "legacy": False,
        "corrupt": False,
    }


def _proposal_identity(
    *,
    slug: str,
    tid: str,
    source_sha256: str,
    base_version: int,
    base_sha256: str | None,
    edited: dict,
) -> str:
    canonical = json.dumps({
        "schema": "figure_review_proposal_v1",
        "slug": slug,
        "task_id": tid,
        "source_sha256": source_sha256,
        "base_version": base_version,
        "base_sha256": base_sha256,
        "edited": edited,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical)


def _strip_internal_meta(d: dict) -> dict:
    """Remove fields chem_reviewer adds itself (so diff isn't polluted)."""
    if not isinstance(d, dict):
        return d
    out = dict(d)
    out.pop("_reviewed_meta", None)
    return out


def _reviewed_versions(d: Path, tid: str) -> list[dict]:
    prefix = f"{tid}_predicted_output.reviewed-v"
    head = _review_head(d, tid, strict=False)
    head_version = head["version"]
    versions = []
    for fp in d.glob(f"{prefix}*.json"):
        match = re.fullmatch(
            rf"{re.escape(prefix)}(\d+)\.json",
            fp.name,
        )
        if not match:
            continue
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
            meta = payload.get("_reviewed_meta") if isinstance(payload, dict) else {}
        except Exception:
            meta = {}
        versions.append({
            "version": int(match.group(1)),
            "reviewed_at": meta.get("reviewed_at"),
            "n_ops": meta.get("n_ops"),
            "parent_version": meta.get("parent_version"),
            "parent_legacy_sha256": meta.get("parent_legacy_sha256"),
            "parent_inferred": meta.get("parent_inferred", False),
            "is_head": (
                None if head["corrupt"]
                else int(match.group(1)) == head_version
            ),
            "file": fp.name,
        })
    return sorted(versions, key=lambda item: item["version"])


def _preview_task_edit(
    d: Path,
    tid: str,
    data: dict,
) -> tuple[dict, dict, list, str, str, dict]:
    orig_fp = predicted_path(d, tid)
    if not orig_fp.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no {tid}_predicted_output.json",
        )
    original = json.loads(orig_fp.read_text(encoding="utf-8"))
    patch = _strip_internal_meta(dict(data))
    merged = deep_merge_preserving(original, patch)
    edited_clean = _strip_internal_meta(merged)
    ops = compute_json_diff(original, edited_clean)
    md = compute_markdown_diff(original, edited_clean)
    source_sha256 = _sha256_bytes(orig_fp.read_bytes())
    return original, merged, ops, md, source_sha256, _review_head(d, tid)


def _legacy_archive_paths(d: Path, tid: str, digest: str) -> tuple[Path, Path, Path]:
    stem = f"{tid}_predicted_output.reviewed-legacy-{digest}"
    return (
        d / f"{stem}.json",
        d / f"{stem}.diff.json",
        d / f"{stem}.diff.md",
    )


def _ensure_legacy_archive(
    d: Path,
    tid: str,
    original: dict,
    head: dict,
) -> None:
    digest = head["sha256"]
    if not head["legacy"] or not digest or not isinstance(head["payload"], dict):
        return
    snapshot, diff_json, diff_md = _legacy_archive_paths(d, tid, digest)
    source_bytes = head["raw_bytes"]
    if not isinstance(source_bytes, bytes) or _sha256_bytes(source_bytes) != digest:
        raise HTTPException(409, "legacy review pointer changed during preview")
    if snapshot.exists():
        if _sha256_bytes(snapshot.read_bytes()) != digest:
            raise HTTPException(409, "legacy review archive hash mismatch")
    else:
        try:
            with snapshot.open("xb") as stream:
                stream.write(source_bytes)
        except FileExistsError:
            if _sha256_bytes(snapshot.read_bytes()) != digest:
                raise HTTPException(409, "legacy review archive collision") from None
    legacy_clean = _strip_internal_meta(head["payload"])
    ops = compute_json_diff(original, legacy_clean)
    markdown = compute_markdown_diff(original, legacy_clean)
    _atomic_write_text(diff_json, json.dumps({
        "_meta": {
            "legacy": True,
            "source_sha256": digest,
            "n_ops": len(ops),
        },
        "ops": ops,
    }, ensure_ascii=False, indent=2) + "\n")
    _atomic_write_text(diff_md, markdown)


@app.post("/api/paper/{slug}/task/{tid}/preview")
def paper_task_preview(slug: str, tid: str, req: SaveRequest):
    """Compute a proposed human edit without writing any reviewer sidecar."""
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    _, merged, ops, md, source_sha256, head = _preview_task_edit(d, tid, req.data)
    versions = _reviewed_versions(d, tid)
    next_version = (versions[-1]["version"] if versions else 0) + 1
    proposal_id = _proposal_identity(
        slug=slug,
        tid=tid,
        source_sha256=source_sha256,
        base_version=head["version"],
        base_sha256=head["sha256"],
        edited=_strip_internal_meta(merged),
    )
    return {
        "schema": "figure_review_proposal_v1",
        "is_reviewed": False,
        "proposed": True,
        "proposal_id": proposal_id,
        "source_sha256": source_sha256,
        "base_version": head["version"],
        "base_sha256": head["sha256"],
        "n_ops": len(ops),
        "ops": ops,
        "markdown": md,
        "next_version": next_version,
    }


@app.post("/api/paper/{slug}/task/{tid}/save")
def paper_task_save(slug: str, tid: str, req: ApplyRequest):
    """Write reviewed sidecar via deep-merge against the ORIGINAL file.

    Critical: we merge the UI patch onto the on-disk original (NOT against
    a previous reviewed copy). This means even if the UI fails to round-
    trip a v2 field, we still preserve it from the source. The cost is
    that re-edits don't cascade — but our UI sends the FULL current
    `taskData` (which we earlier fed it from disk including all v2
    fields), so this is the safer default.
    """
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    if not re.fullmatch(r"[0-9a-f]{64}", req.proposal_id):
        raise HTTPException(400, "invalid review proposal id")
    if (
        req.agent_proposal_id is not None
        and not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", req.agent_proposal_id)
    ):
        raise HTTPException(400, "invalid agent proposal id")
    original, merged, ops, md, source_sha256, head = _preview_task_edit(
        d, tid, req.data,
    )
    expected_proposal_id = _proposal_identity(
        slug=slug,
        tid=tid,
        source_sha256=source_sha256,
        base_version=head["version"],
        base_sha256=head["sha256"],
        edited=_strip_internal_meta(merged),
    )
    if not hmac.compare_digest(req.proposal_id, expected_proposal_id):
        raise HTTPException(
            409,
            "review proposal is stale; preview the current task again",
        )
    _ensure_legacy_archive(d, tid, original, head)
    versions = _reviewed_versions(d, tid)
    version = (versions[-1]["version"] if versions else 0) + 1
    merged["_reviewed_meta"] = {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reviewer_tool": "chem_reviewer v0.3",
        "slug": slug,
        "task_id": tid,
        "version": version,
        "parent_version": head["version"] or None,
        "parent_legacy_sha256": head["sha256"] if head["legacy"] else None,
        "proposal_id": expected_proposal_id,
        **(
            {"agent_proposal_id": req.agent_proposal_id}
            if req.agent_proposal_id is not None else {}
        ),
        "source_sha256": source_sha256,
        "n_ops": len(ops),
    }

    payload = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    version_fp = d / f"{tid}_predicted_output.reviewed-v{version}.json"
    # Immutable snapshots use exclusive creation. A concurrent reviewer that
    # wins the same version number forces this request to retry explicitly.
    try:
        with version_fp.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    except FileExistsError:
        raise HTTPException(409, "review version advanced; preview changes again") from None

    out_fp = reviewed_path(d, tid)
    diff_json_fp = diff_json_path(d, tid)
    diff_md_fp = diff_md_path(d, tid)
    diff_json_payload = {
        "_meta": {
            "slug": slug,
            "task_id": tid,
            "reviewed_at": merged["_reviewed_meta"]["reviewed_at"],
            "reviewer_tool": "chem_reviewer v0.3",
            "n_ops": len(ops),
            "version": version,
            "parent_version": head["version"] or None,
            "parent_legacy_sha256": (
                head["sha256"] if head["legacy"] else None
            ),
            "proposal_id": expected_proposal_id,
            **(
                {"agent_proposal_id": req.agent_proposal_id}
                if req.agent_proposal_id is not None else {}
            ),
            "source_sha256": source_sha256,
        },
        "ops": ops,
    }
    diff_json_payload_text = json.dumps(
        diff_json_payload, ensure_ascii=False, indent=2,
    ) + "\n"
    diff_md_payload = (
        f"<!-- {slug} · {tid} · v{version} · "
        f"{merged['_reviewed_meta']['reviewed_at']} -->\n\n" + md
    )
    version_diff_json = d / f"{tid}_predicted_output.reviewed-v{version}.diff.json"
    version_diff_md = d / f"{tid}_predicted_output.reviewed-v{version}.diff.md"
    _atomic_write_text(version_diff_json, diff_json_payload_text)
    _atomic_write_text(version_diff_md, diff_md_payload)
    _atomic_write_text(diff_json_fp, diff_json_payload_text)
    _atomic_write_text(diff_md_fp, diff_md_payload)
    # The compatibility pointer is the commit marker. Publish it only after
    # its immutable snapshot and both human/machine diff artifacts exist.
    _atomic_write_text(out_fp, payload)

    return {
        "ok": True,
        # The data root is normally outside the repository. Return stable
        # sidecar names instead of leaking an absolute local path.
        "path": out_fp.name,
        "diff_json": diff_json_fp.name,
        "diff_md": diff_md_fp.name,
        "n_ops": len(ops),
        "reviewed_at": merged["_reviewed_meta"]["reviewed_at"],
        "version": version,
        "parent_version": head["version"] or None,
        "parent_legacy_sha256": head["sha256"] if head["legacy"] else None,
        "proposal_id": expected_proposal_id,
        "agent_proposal_id": req.agent_proposal_id,
        "snapshot": version_fp.name,
    }


@app.get("/api/paper/{slug}/task/{tid}/versions")
def paper_task_versions(slug: str, tid: str):
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    return {"versions": _reviewed_versions(d, tid)}


@app.get("/api/paper/{slug}/task/{tid}/versions/{version}/diff")
def paper_task_version_diff(slug: str, tid: str, version: int):
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    if version < 1:
        raise HTTPException(404, "review version not found")
    json_fp = d / f"{tid}_predicted_output.reviewed-v{version}.diff.json"
    md_fp = d / f"{tid}_predicted_output.reviewed-v{version}.diff.md"
    if not json_fp.exists() or not md_fp.exists():
        raise HTTPException(404, "review version diff not found")
    payload = json.loads(json_fp.read_text(encoding="utf-8"))
    return {
        "is_reviewed": True,
        "version": version,
        "n_ops": payload.get("_meta", {}).get("n_ops", len(payload.get("ops", []))),
        "ops": payload.get("ops", []),
        "markdown": md_fp.read_text(encoding="utf-8"),
        "from_cache": True,
    }


@app.post("/api/paper/{slug}/task/{tid}/versions/{version}/restore")
def paper_task_version_restore(slug: str, tid: str, version: int):
    """Recover a damaged compatibility pointer from an immutable snapshot."""
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    if version < 1:
        raise HTTPException(404, "review version not found")
    original_fp = predicted_path(d, tid)
    snapshot = d / f"{tid}_predicted_output.reviewed-v{version}.json"
    version_diff_json = d / f"{tid}_predicted_output.reviewed-v{version}.diff.json"
    version_diff_md = d / f"{tid}_predicted_output.reviewed-v{version}.diff.md"
    if not original_fp.exists() or not all(fp.exists() for fp in (
        snapshot, version_diff_json, version_diff_md,
    )):
        raise HTTPException(409, "immutable review version is incomplete")
    try:
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(409, "immutable review snapshot is unreadable") from exc
    meta = payload.get("_reviewed_meta", {}) if isinstance(payload, dict) else {}
    if meta.get("version") != version:
        raise HTTPException(409, "immutable review version metadata mismatch")
    source_sha256 = meta.get("source_sha256")
    if (
        source_sha256
        and source_sha256 != _sha256_bytes(original_fp.read_bytes())
    ):
        raise HTTPException(409, "raw prediction changed since this review version")
    _atomic_write_text(
        diff_json_path(d, tid),
        version_diff_json.read_text(encoding="utf-8"),
    )
    _atomic_write_text(
        diff_md_path(d, tid),
        version_diff_md.read_text(encoding="utf-8"),
    )
    _atomic_write_bytes(reviewed_path(d, tid), snapshot.read_bytes())
    return {
        "ok": True,
        "head_version": version,
        "is_reviewed": True,
    }


@app.post("/api/paper/{slug}/task/{tid}/undo")
def paper_task_reviewed_undo(slug: str, tid: str):
    """Move the Figure review head to its immutable parent version."""
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    head = _review_head(d, tid)
    if not head["version"]:
        if not head["legacy"]:
            raise HTTPException(409, "task is already at the raw prediction")
        for fp in (diff_json_path(d, tid), diff_md_path(d, tid)):
            if fp.exists():
                fp.unlink()
        pointer = reviewed_path(d, tid)
        if pointer.exists():
            pointer.unlink()
        return {
            "ok": True,
            "undone_version": "legacy",
            "head_version": 0,
            "head_kind": "raw",
            "parent_inferred": False,
            "is_reviewed": False,
        }
    parent_version = head["parent_version"]
    parent_legacy_sha256 = head["parent_legacy_sha256"]
    parent_inferred = False
    if not isinstance(parent_version, int):
        if parent_legacy_sha256:
            legacy_snapshot, legacy_diff_json, legacy_diff_md = (
                _legacy_archive_paths(d, tid, parent_legacy_sha256)
            )
            if not all(fp.exists() for fp in (
                legacy_snapshot, legacy_diff_json, legacy_diff_md,
            )):
                raise HTTPException(409, "legacy parent review is incomplete")
            if _sha256_bytes(legacy_snapshot.read_bytes()) != parent_legacy_sha256:
                raise HTTPException(409, "legacy parent review hash mismatch")
            _atomic_write_text(
                diff_json_path(d, tid),
                legacy_diff_json.read_text(encoding="utf-8"),
            )
            _atomic_write_text(
                diff_md_path(d, tid),
                legacy_diff_md.read_text(encoding="utf-8"),
            )
            _atomic_write_bytes(
                reviewed_path(d, tid),
                legacy_snapshot.read_bytes(),
            )
            return {
                "ok": True,
                "undone_version": head["version"],
                "head_version": None,
                "head_kind": "legacy",
                "parent_inferred": False,
                "is_reviewed": True,
            }
        # Compatibility for reviewed-vN snapshots written before parent links.
        parent_version = max(0, head["version"] - 1)
        parent_inferred = True

    pointer = reviewed_path(d, tid)
    current_diff_json = diff_json_path(d, tid)
    current_diff_md = diff_md_path(d, tid)
    if parent_version:
        parent_snapshot = d / (
            f"{tid}_predicted_output.reviewed-v{parent_version}.json"
        )
        parent_diff_json = d / (
            f"{tid}_predicted_output.reviewed-v{parent_version}.diff.json"
        )
        parent_diff_md = d / (
            f"{tid}_predicted_output.reviewed-v{parent_version}.diff.md"
        )
        if not all(fp.exists() for fp in (
            parent_snapshot, parent_diff_json, parent_diff_md,
        )):
            raise HTTPException(409, "parent review version is incomplete")
        _atomic_write_text(
            current_diff_json,
            parent_diff_json.read_text(encoding="utf-8"),
        )
        _atomic_write_text(
            current_diff_md,
            parent_diff_md.read_text(encoding="utf-8"),
        )
        # As in Apply, the compatibility pointer is the commit marker.
        _atomic_write_bytes(pointer, parent_snapshot.read_bytes())
    else:
        for fp in (current_diff_json, current_diff_md):
            if fp.exists():
                fp.unlink()
        if pointer.exists():
            pointer.unlink()
    return {
        "ok": True,
        "undone_version": head["version"],
        "head_version": parent_version,
        "head_kind": "version" if parent_version else "raw",
        "parent_inferred": parent_inferred,
        "is_reviewed": bool(parent_version),
    }


def _atomic_write_text(target: Path, payload: str) -> None:
    _atomic_write_bytes(target, payload.encode("utf-8"))


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@app.get("/api/paper/{slug}/task/{tid}/diff")
def paper_task_diff(slug: str, tid: str):
    """Return cached diff sidecars; recompute live if missing."""
    d = _paper_dir(slug)
    tid = _safe_segment(tid)
    orig_fp = predicted_path(d, tid)
    rev_fp = reviewed_path(d, tid)
    diff_json_fp = diff_json_path(d, tid)
    diff_md_fp = diff_md_path(d, tid)

    if not orig_fp.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no {tid}_predicted_output.json: {slug}/{tid}",
        )

    if not rev_fp.exists():
        return {
            "is_reviewed": False,
            "n_ops": 0,
            "ops": [],
            "markdown": "**没有审核记录(reviewed sidecar 不存在)。**\n",
        }

    if diff_json_fp.exists() and diff_md_fp.exists():
        try:
            d_json = json.loads(diff_json_fp.read_text(encoding="utf-8"))
            d_md = diff_md_fp.read_text(encoding="utf-8")
            cache_meta = d_json.get("_meta", {})
            head = _review_head(d, tid, strict=False)
            cache_matches = (
                not head["corrupt"]
                and (
                    (
                        not head["legacy"]
                        and cache_meta.get("version") == head["version"]
                    )
                    or (
                        head["legacy"]
                        and cache_meta.get("legacy") is True
                        and cache_meta.get("source_sha256") == head["sha256"]
                    )
                )
            )
            if cache_matches:
                return {
                    "is_reviewed": True,
                    "version": cache_meta.get("version"),
                    "n_ops": cache_meta.get("n_ops", len(d_json.get("ops", []))),
                    "ops": d_json.get("ops", []),
                    "markdown": d_md,
                    "from_cache": True,
                }
        except Exception:
            pass

    original = json.loads(orig_fp.read_text(encoding="utf-8"))
    try:
        reviewed = json.loads(rev_fp.read_text(encoding="utf-8"))
    except Exception:
        return {
            "is_reviewed": False,
            "review_pointer_corrupt": True,
            "history_available": bool(_reviewed_versions(d, tid)),
            "n_ops": 0,
            "ops": [],
            "markdown": (
                "**Review pointer is damaged. Immutable version history "
                "is still available for explicit recovery.**\n"
            ),
            "from_cache": False,
        }
    edited_clean = _strip_internal_meta(reviewed)
    ops = compute_json_diff(original, edited_clean)
    md = compute_markdown_diff(original, edited_clean)
    return {
        "is_reviewed": True,
        "version": reviewed.get("_reviewed_meta", {}).get("version"),
        "n_ops": len(ops),
        "ops": ops,
        "markdown": md,
        "from_cache": False,
    }


# ─────────────────────── Phase 5 Plan 05-01: stereo round-trip ───────────────────────
# Monitor (:8772) lists `_source.stereo_uncertain: true` examples on its
# Stereo-Uncertain tab → chemist clicks "→ Review in Ketcher" → opens
# this server (:8773) at /stereo_review?exp=...&example=...&anchor=... with
# the SMILES auto-loaded into Ketcher. Chemist redraws + saves → POST
# /save_stereo_resolution writes <work_dir>/<anchor>_predicted_output.reviewed.json
# atomically (tempfile + os.replace per Pitfall 13). The ORIGINAL pred file
# is NEVER mutated (Pitfall 6 / D-01 — provenance chain preserved).

_STEREO_EXP_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")
_VALID_STEREO_ACTIONS = {"chemist_resolved", "chemist_confirmed_unknowable"}


def _sanitize_exp_id_stereo(exp_id: Optional[str]) -> str:
    if not exp_id or not _STEREO_EXP_ID_RE.match(exp_id):
        raise HTTPException(400, f"invalid exp id (path traversal blocked): {exp_id!r}")
    return exp_id


def _atomic_write_json_sidecar(payload: dict, final_path: Path) -> None:
    """Atomic write via tempfile + os.replace, same FS as final (Pitfall 13).

    Mirrors agent/merge_anchors_to_paper.py::_atomic_write_json. The prefix
    `.reviewed.` makes orphan tmp files easy to identify after a crash.
    """
    out_dir = final_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".reviewed.", suffix=".tmp", dir=str(out_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(final_path))
    except Exception:
        # Best-effort cleanup of orphan tmp file
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_sidecar_or_empty(sidecar_path: Path) -> dict:
    """Read existing sidecar (if any) so we merge new resolution rather than overwrite.

    Retry-once on JSONDecodeError to tolerate concurrent reader/writer (D-05).
    """
    if not sidecar_path.exists():
        return {"selected_examples": []}
    for attempt in range(2):
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"selected_examples": []}
            data.setdefault("selected_examples", [])
            if not isinstance(data["selected_examples"], list):
                data["selected_examples"] = []
            return data
        except json.JSONDecodeError:
            if attempt == 0:
                import time as _t
                _t.sleep(0.05)
                continue
            print(f"[chem_reviewer] sidecar parse failed twice; starting fresh: {sidecar_path}", file=sys.stderr)
            return {"selected_examples": []}
        except OSError as e:
            print(f"[chem_reviewer] sidecar read failed: {sidecar_path}: {e}", file=sys.stderr)
            return {"selected_examples": []}
    return {"selected_examples": []}


def _stereo_pred_path(work_dir: Path, anchor_idx: int) -> Path:
    return work_dir / f"{anchor_idx}_predicted_output.json"


def _stereo_sidecar_path(work_dir: Path, anchor_idx: int) -> Path:
    return work_dir / f"{anchor_idx}_predicted_output.reviewed.json"


def _count_resolved_in_sidecar(sidecar_data: dict) -> int:
    n = 0
    for ex in (sidecar_data.get("selected_examples") or []):
        if not isinstance(ex, dict):
            continue
        for p in (ex.get("products") or []):
            src = (p or {}).get("_source") or {}
            if src.get("stereo_uncertain") is False:
                n += 1
                break
        else:
            ex_src = (ex.get("_source") or {})
            if ex_src.get("stereo_uncertain") is False:
                n += 1
    return n


class SaveStereoRequest(BaseModel):
    exp_id: str
    work_dir: str
    anchor_idx: int = 1
    example_idx: int
    new_smi: str
    action: str = "chemist_resolved"


@app.post("/save_stereo_resolution")
def save_stereo_resolution(req: SaveStereoRequest):
    """Atomic sidecar write — original pred file NEVER touched (D-01 + D-06).

    Sidecar lifecycle:
      - First call → create sidecar with the resolved example
      - Subsequent calls → MERGE into existing sidecar (preserve other
        resolutions; never drop)
      - Atomic: tempfile.mkstemp(dir=parent) → os.replace (Pitfall 13)
      - _strip_history appended (append-only; Phase 1 D-01 + Plan 05 D-03)
    """
    exp_id = _sanitize_exp_id_stereo(req.exp_id)
    if req.action not in _VALID_STEREO_ACTIONS:
        raise HTTPException(400, f"action must be one of {_VALID_STEREO_ACTIONS}; got {req.action!r}")

    work_dir = Path(req.work_dir)
    if not work_dir.exists() or not work_dir.is_dir():
        raise HTTPException(404, f"work_dir not found: {req.work_dir}")

    pred_path = _stereo_pred_path(work_dir, req.anchor_idx)
    if not pred_path.exists():
        raise HTTPException(404, f"pred file not found: {pred_path}")

    # Read pred (defensive — also locked in the mtime sentinel below)
    try:
        pred = json.loads(pred_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(500, f"pred file unreadable: {e}")

    examples = pred.get("selected_examples") or []
    if not isinstance(examples, list):
        raise HTTPException(500, "pred file shape invalid: selected_examples not a list")
    if req.example_idx < 0 or req.example_idx >= len(examples):
        raise HTTPException(400, f"example_idx {req.example_idx} out of range (0..{len(examples)-1})")

    pred_ex = examples[req.example_idx]
    pred_prod = (pred_ex.get("products") or [{}])[0]
    original_smi = pred_prod.get("E-SMILES") or pred_prod.get("smi") or ""
    instance_id = pred_ex.get("instance_id") or f"ex_{req.example_idx}"

    # Pitfall 6: capture pred mtime BEFORE write to assert we don't mutate it
    pred_mtime_before = pred_path.stat().st_mtime_ns

    # Load existing sidecar + merge
    sidecar_path = _stereo_sidecar_path(work_dir, req.anchor_idx)
    sidecar = _load_sidecar_or_empty(sidecar_path)
    if "selected_examples" not in sidecar or not isinstance(sidecar["selected_examples"], list):
        sidecar["selected_examples"] = []

    # Find or create the target example in sidecar (by instance_id, fallback by example_idx)
    target = None
    for s_ex in sidecar["selected_examples"]:
        if not isinstance(s_ex, dict):
            continue
        if s_ex.get("instance_id") == instance_id and instance_id:
            target = s_ex
            break
        if s_ex.get("_anchor_idx") == req.anchor_idx and s_ex.get("_example_idx") == req.example_idx:
            target = s_ex
            break
    if target is None:
        target = {
            "instance_id": instance_id,
            "_anchor_idx": req.anchor_idx,
            "_example_idx": req.example_idx,
            "products": [{
                "identifier": pred_prod.get("identifier") or instance_id,
                "E-SMILES": req.new_smi,
                "_source": {},
            }],
        }
        sidecar["selected_examples"].append(target)

    # Ensure product exists
    if not target.get("products"):
        target["products"] = [{"identifier": instance_id, "E-SMILES": req.new_smi, "_source": {}}]
    prod = target["products"][0]
    prod["E-SMILES"] = req.new_smi
    src = prod.setdefault("_source", {})
    now_iso = datetime.now(timezone.utc).isoformat()
    src["stereo_uncertain"] = False
    src["stereo_status"] = req.action
    src["stereo_reviewed_at"] = now_iso
    src["stereo_reviewed_by"] = "chemist"

    # Append-only history (D-06 + Phase 1 D-01)
    hist = src.get("_strip_history")
    if not isinstance(hist, list):
        hist = []
    hist.append({
        "when": now_iso,
        "action": req.action,
        "original_smi": original_smi,
        "new_smi": req.new_smi,
        "reviewer": "chemist",
        "exp_id": exp_id,
    })
    src["_strip_history"] = hist

    # Sidecar-level meta
    sidecar["_reviewed_meta"] = {
        "exp_id": exp_id,
        "anchor_idx": req.anchor_idx,
        "last_reviewed_at": now_iso,
        "reviewer_tool": "chem_reviewer stereo-roundtrip v1",
    }

    # Atomic write
    _atomic_write_json_sidecar(sidecar, sidecar_path)

    # Pitfall 6 defensive assertion: pred mtime unchanged
    pred_mtime_after = pred_path.stat().st_mtime_ns
    pred_untouched = (pred_mtime_after == pred_mtime_before)
    if not pred_untouched:
        # Should be impossible — we only write the sidecar path. But surface loudly.
        print(
            f"[chem_reviewer] FATAL: pred file mtime changed after save_stereo_resolution! "
            f"before={pred_mtime_before} after={pred_mtime_after} path={pred_path}",
            file=sys.stderr,
        )

    return {
        "ok": True,
        "sidecar_path": str(sidecar_path),
        "resolved_count": _count_resolved_in_sidecar(sidecar),
        "instance_id": instance_id,
        "stereo_reviewed_at": now_iso,
        "pred_untouched": pred_untouched,
    }


# ── Deep-link landing page for ?exp=&example=&anchor= ────────────────
_STEREO_REVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>chem_reviewer · stereo review — {exp} ex {example}</title>
<style>
  :root {{
    --bg: #FAF7F2; --card: #FFFFFF; --ink: #1F1E1C; --muted: #8A8275;
    --orange: #D97757; --orange-2: #C25A3A; --border: #E8E0D2;
  }}
  body {{
    font-family: 'Manrope', system-ui, sans-serif;
    background: var(--bg); color: var(--ink); margin: 0; padding: 24px;
  }}
  .card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 24px; max-width: 720px; margin: 0 auto;
  }}
  h1 {{ font-family: 'Fraunces', Georgia, serif; font-weight: 500; }}
  .label {{ color: var(--muted); font-size: 13px; }}
  .val {{ font-family: 'JetBrains Mono', monospace; font-size: 14px; word-break: break-all; }}
  .row {{ margin: 10px 0; }}
  button, a.btn {{
    display: inline-block; padding: 10px 16px; background: var(--orange);
    color: white; border: none; border-radius: 6px; font-weight: 500;
    text-decoration: none; cursor: pointer; font-size: 14px;
  }}
  button.confirm-unknowable {{ background: var(--muted); margin-left: 8px; }}
  textarea {{
    width: 100%; min-height: 80px; padding: 8px; border: 1px solid var(--border);
    border-radius: 4px; font-family: 'JetBrains Mono', monospace; font-size: 13px;
  }}
  .msg {{ margin-top: 12px; padding: 12px; border-radius: 4px; }}
  .msg.ok {{ background: #E8F2EB; color: #2D7D5C; }}
  .msg.err {{ background: #FAE9E9; color: #B14545; }}
</style>
</head>
<body>
<div class="card">
  <h1>Stereo review</h1>
  <div class="row"><span class="label">experiment:</span> <span class="val" id="exp">{exp}</span></div>
  <div class="row"><span class="label">anchor:</span> <span class="val" id="anchor">{anchor}</span></div>
  <div class="row"><span class="label">example_idx:</span> <span class="val" id="example">{example}</span></div>
  <div class="row">
    <div class="label">work_dir (chemist must paste — monitor doesn't know paths)</div>
    <input id="work_dir" type="text" style="width:100%;padding:8px;font-family:'JetBrains Mono',monospace;font-size:13px"
           placeholder="/path/to/work_dir"/>
  </div>
  <div class="row">
    <div class="label">corrected SMILES (drawn in Ketcher → paste here)</div>
    <textarea id="new_smi" placeholder="paste corrected SMILES"></textarea>
  </div>
  <div class="row">
    <button onclick="saveResolution('chemist_resolved')">Save chemist-resolved</button>
    <button class="confirm-unknowable" onclick="saveResolution('chemist_confirmed_unknowable')">
      Confirm unknowable (agent should not re-strip)
    </button>
  </div>
  <div id="msg"></div>
</div>
<script>
  window.STEREO_PARAMS = {{
    exp_id: "{exp}",
    example_idx: {example_int},
    anchor_idx: {anchor_int},
  }};
  async function saveResolution(action) {{
    const new_smi = document.getElementById('new_smi').value.trim();
    const work_dir = document.getElementById('work_dir').value.trim();
    if (!new_smi) {{ showMsg('paste a SMILES first', 'err'); return; }}
    if (!work_dir) {{ showMsg('paste the work_dir path first', 'err'); return; }}
    const payload = {{
      exp_id: window.STEREO_PARAMS.exp_id,
      work_dir: work_dir,
      anchor_idx: window.STEREO_PARAMS.anchor_idx,
      example_idx: window.STEREO_PARAMS.example_idx,
      new_smi: new_smi,
      action: action,
    }};
    try {{
      const r = await fetch('/save_stereo_resolution', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload),
      }});
      const j = await r.json();
      if (r.ok) showMsg('✓ saved · resolved=' + j.resolved_count + ' · sidecar=' + j.sidecar_path, 'ok');
      else showMsg('save failed: ' + (j.detail || JSON.stringify(j)), 'err');
    }} catch (e) {{ showMsg('save error: ' + e, 'err'); }}
  }}
  function showMsg(t, cls) {{
    const m = document.getElementById('msg');
    m.className = 'msg ' + cls;
    m.textContent = t;
  }}
</script>
</body>
</html>
"""


@app.get("/stereo_review", response_class=HTMLResponse)
def stereo_review_page(
    exp: Optional[str] = None,
    example: Optional[int] = None,
    anchor: Optional[int] = 1,
):
    """Deep-link landing page (D-03). Accepts ?exp=...&example=...&anchor=...

    The monitor (:8772) Stereo-Uncertain tab links here. Body echoes the
    parameters as `window.STEREO_PARAMS` so client-side Ketcher (when wired)
    can auto-load the current SMILES from the monitor's pred file.
    """
    safe_exp = exp or ""
    if safe_exp and not _STEREO_EXP_ID_RE.match(safe_exp):
        raise HTTPException(400, "invalid exp id")
    ex_int = int(example) if (example is not None) else 0
    an_int = int(anchor) if (anchor is not None) else 1
    return HTMLResponse(content=_STEREO_REVIEW_HTML.format(
        exp=safe_exp, example=ex_int, anchor=an_int,
        example_int=ex_int, anchor_int=an_int,
    ))


# ─────────────────────── /Phase 5 Plan 05-01 ───────────────────────


# ── Entry ─────────────────────────────────────────────────────────────


def main() -> None:
    global DATA_ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--data-root", type=Path, default=None,
                        help="external paper/review sidecar root (overrides PDF_ROOT)")
    args = parser.parse_args()
    if args.data_root is not None:
        DATA_ROOT = args.data_root.expanduser().resolve()
    if not DATA_ROOT.is_dir():
        parser.error(f"data root does not exist: {DATA_ROOT}")

    print(f"[chem_reviewer] data root: {DATA_ROOT}")
    print(f"[chem_reviewer] ketcher  : {KETCHER_DIR} "
          f"({'OK' if KETCHER_DIR.exists() else 'MISSING'})")
    print(f"[chem_reviewer] web      : {WEB_DIR}")
    print(f"[chem_reviewer] starting at http://{args.host}:{args.port}/")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
