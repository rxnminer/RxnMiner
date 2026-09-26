"""v2 schema loader and helpers for chem_reviewer.

Reads ``PDF/<paper>/N_predicted_output.json`` (hujinbo v2 schema) verbatim,
extracts task-level metadata, builds molparser-detection lists from inline
``_source.bbox`` provenance, and builds cytoscape-ready multistep graphs from
``reaction_graph[]`` + ``synthesis_overview``.

Pure read helpers — never mutate input dicts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ── v2 file naming ─────────────────────────────────────────────────────
_PRED_RE = re.compile(r"^(\d+)_predicted_output\.json$")
SIDECAR_SUFFIX = "_predicted_output.reviewed.json"
DIFF_JSON_SUFFIX = "_predicted_output.reviewed.diff.json"
DIFF_MD_SUFFIX = "_predicted_output.reviewed.diff.md"


def task_id_from_filename(fname: str) -> str | None:
    """``"2_predicted_output.json"`` → ``"2"``. Returns None if not a v2 file."""
    m = _PRED_RE.match(fname)
    return m.group(1) if m else None


def predicted_path(paper_dir: Path, tid: str) -> Path:
    return paper_dir / f"{tid}_predicted_output.json"


def reviewed_path(paper_dir: Path, tid: str) -> Path:
    return paper_dir / f"{tid}{SIDECAR_SUFFIX}"


def diff_json_path(paper_dir: Path, tid: str) -> Path:
    return paper_dir / f"{tid}{DIFF_JSON_SUFFIX}"


def diff_md_path(paper_dir: Path, tid: str) -> Path:
    return paper_dir / f"{tid}{DIFF_MD_SUFFIX}"


def list_predicted_files(paper_dir: Path) -> list[Path]:
    """All ``N_predicted_output.json`` in a paper dir, sorted by N (numeric)."""
    out = []
    for p in paper_dir.iterdir():
        if not p.is_file():
            continue
        tid = task_id_from_filename(p.name)
        if tid is None:
            continue
        out.append((int(tid), p))
    out.sort(key=lambda x: x[0])
    return [p for _, p in out]


# ── PDF discovery ──────────────────────────────────────────────────────


def pdf_path_for_paper(data_root: Path, slug: str) -> Path:
    """``PDF/<slug>/`` 旁边的 ``PDF/<slug>.pdf``."""
    return data_root / f"{slug}.pdf"


def paper_meta_path(paper_dir: Path) -> Path:
    """``PDF/<slug>/<slug>.json`` — pdf screenshotter metadata.

    Holds ``page_count`` + ``screenshots[]`` (anchor PNG bbox in PDF points).
    """
    return paper_dir / f"{paper_dir.name}.json"


def reactions_overview_path(paper_dir: Path) -> Path:
    """``PDF/<slug>/reactions.json`` — paper-level overview (title, doi, ...)."""
    return paper_dir / "reactions.json"


# ── Per-task field extraction ──────────────────────────────────────────


def extract_task_meta(data: dict, tid: str) -> dict:
    """Pull manifest-relevant fields from a v2 ``N_predicted_output.json``.

    Returned shape mirrors what the old ``paper_manifest.json`` per-task entry
    used to give chem_reviewer's TaskNav, plus the new v2-only flags.
    """
    ov = data.get("synthesis_overview") or {}
    n_failed_examples = sum(
        1 for ex in (data.get("selected_examples") or [])
        if (ex.get("_outcome") or {}).get("is_failed") is True
    )
    n_failed_templates = sum(
        1 for t in (data.get("reaction_templates") or [])
        if (t.get("_outcome") or {}).get("is_failed") is True
    )
    return {
        "task_id": tid,
        "anchor_image": data.get("anchor_image", f"{tid}.png"),
        "anchor_figure": data.get("anchor_figure", ""),
        "anchor_page": data.get("anchor_page"),
        "anchor_bbox_pdf": data.get("anchor_bbox_pdf"),
        "molparser_detection_count": data.get("molparser_detection_count", 0),
        "n_templates": len(data.get("reaction_templates") or []),
        "n_examples": len(data.get("selected_examples") or []),
        "n_dag_edges": len(data.get("reaction_graph") or []),
        "n_failed": n_failed_examples + n_failed_templates,
        "is_multistep_synthesis": bool(data.get("is_multistep_synthesis")),
        "synthesis_type": ov.get("type"),
        "synthesis_name": ov.get("name"),
        "extractor": data.get("extractor", ""),
        # Back-compat aliases used by current frontend (TaskNav reads
        # these for layout). When missing the v2 anchor_figure is shown
        # alone (no subpanel concept in v2).
        "source_caption": data.get("anchor_figure", ""),
        "source_subpanel": "",
        "page_index": data.get("anchor_page"),
        "mp_count": data.get("molparser_detection_count", 0),
    }


# ── molparser detection aggregation ────────────────────────────────────

_MOLPARSER_TYPES = {"image_structure"}


def collect_molparser_detections(data: dict) -> list[dict]:
    """Gather all ``_source.type=image_structure`` molecules into a flat list.

    Same shape the old ``extracted/papers/.../molparser_anchors/<tid>.json``
    produced for the SPA's bbox overlay. Dedup by ``bbox_index``.
    """
    seen: set = set()
    out: list[dict] = []

    def visit_mol_list(mols: list, role_hint: str = "") -> None:
        for mol in mols or []:
            if not isinstance(mol, dict):
                continue
            src = mol.get("_source") or {}
            # Overlay ANY molecule that carries a drawn-box provenance, not just type=image_structure:
            # template (通式) molecules use type="template" but still have a real bbox from the top scheme,
            # so they were silently dropped from the figure overlay. Collect on bbox presence too.
            if src.get("type") not in _MOLPARSER_TYPES and src.get("bbox") is None and src.get("bbox_index") is None:
                continue
            idx = src.get("bbox_index")
            key = (idx, src.get("molparser_md5"))
            if key in seen:
                continue
            seen.add(key)
            esmiles = mol.get("E-SMILES") or ""
            out.append({
                "bbox": src.get("bbox"),
                "bbox_index": idx,
                "smi": esmiles.split("<sep>")[0] if "<sep>" in esmiles else esmiles,
                "caption": esmiles,
                "md5": src.get("molparser_md5"),
                "identifier": mol.get("identifier") or "",
                "role_hint": role_hint,
                "markush": bool(src.get("molparser_markush")),
            })

    for t in data.get("reaction_templates", []) or []:
        visit_mol_list(t.get("reactants"), "reactant")
        visit_mol_list(t.get("products"), "product")
        for step in t.get("conditions") or []:
            visit_mol_list(step, "condition")
    for ex in data.get("selected_examples", []) or []:
        visit_mol_list(ex.get("reactants"), "reactant")
        visit_mol_list(ex.get("products"), "product")
        for step in ex.get("conditions") or []:
            visit_mol_list(step, "condition")
    return out


def merge_raw_molparser_detections(raw: object, data: dict) -> list[dict]:
    """Return genuine MolParser records enriched with final-V2 display metadata.

    ``collect_molparser_detections`` is a useful fallback for old imports, but its
    ``smi`` is the final V2 molecule and therefore must not be presented as raw
    MolParser output. New imports may carry ``<tid>_molparser.json``; this helper
    preserves its original ``smi``, ``caption``, ``esmi``, score and md5 verbatim,
    while adding only identifier/role hints from the final V2 by bbox index.
    """
    if isinstance(raw, dict):
        raw = raw.get("detections") or raw.get("result") or raw.get("data") or []
    if not isinstance(raw, list):
        return []

    final_by_index = {
        item.get("bbox_index"): item
        for item in collect_molparser_detections(data)
        if item.get("bbox_index") is not None
    }
    out: list[dict] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        det = dict(item)
        idx = det.get("bbox_index", position)
        det["bbox_index"] = idx
        hint = final_by_index.get(idx) or {}
        det.setdefault("bbox", hint.get("bbox"))
        det["identifier"] = hint.get("identifier") or det.get("identifier") or ""
        det["role_hint"] = hint.get("role_hint") or det.get("role_hint") or ""
        det["raw_available"] = True
        out.append(det)
    return out


# ── Deep merge for save (preserves v2 fields not in patch) ─────────────


def deep_merge_preserving(original: Any, patch: Any) -> Any:
    """Apply ``patch`` onto ``original`` recursively.

    Rules:
        * dict + dict: merge keys; for shared keys recurse; patch keys
          missing in original are added; original keys missing in patch
          are KEPT (this is the whole point — UI may not surface every
          v2 field but we mustn't drop them on save).
        * list + list of equal length: align by index, recurse element-wise
          (so MoleculeBlock ``_source`` survives even if UI only edited
          ``E-SMILES``).
        * list + list of differing length: take patch verbatim (UI added/
          removed array elements; we trust UI).
        * mixed types or scalars: take patch (overwrite).
    """
    if isinstance(original, dict) and isinstance(patch, dict):
        out = {}
        # Start with patch order, then add original-only keys at the end
        # (preserves UI's ordering choices for re-arranged dicts).
        for k in patch:
            if k in original:
                out[k] = deep_merge_preserving(original[k], patch[k])
            else:
                out[k] = patch[k]
        for k in original:
            if k not in patch:
                out[k] = original[k]
        return out

    if isinstance(original, list) and isinstance(patch, list):
        if len(original) == len(patch):
            return [
                deep_merge_preserving(original[i], patch[i])
                for i in range(len(original))
            ]
        # Length mismatch — UI added or removed entries. Trust patch
        # entirely; recursing per-index would silently re-introduce
        # deleted items.
        return patch

    return patch


# ── Convenience: load a task with reviewed-overlay logic ───────────────


def load_task(paper_dir: Path, tid: str) -> tuple[dict | None, dict | None, bool]:
    """Return ``(displayed, original, is_reviewed)``.

    * ``original`` = raw ``N_predicted_output.json`` (always)
    * ``displayed`` = reviewed sidecar if present, else == original
    * ``is_reviewed`` = True iff sidecar exists
    """
    orig_fp = predicted_path(paper_dir, tid)
    if not orig_fp.exists():
        return None, None, False
    original = json.loads(orig_fp.read_text(encoding="utf-8"))
    rev_fp = reviewed_path(paper_dir, tid)
    if rev_fp.exists():
        try:
            displayed = json.loads(rev_fp.read_text(encoding="utf-8"))
            return displayed, original, True
        except Exception:
            pass
    return original, original, False
