#!/usr/bin/env python3
"""Self-contained stdio MCP server for the rxn-skills Codex plugin.

The Codex model is the extraction agent. This server supplies deterministic
chemistry tools: OCSR, visual comparison, R-group reverse/fill, V2 assembly,
SMILES verification, and reaction rendering.

Run through ``scripts/run_chem_mcp.py`` so Python dependencies are bootstrapped.
``detect`` uses UniParser snip mode with ``UNIPARSER_API_KEY``, or a hash-bound
OCSR cache for offline extraction.

Stdout is reserved for JSON-RPC. All logs go to stderr.
"""
import sys, os, json, tempfile, traceback, hashlib
from pathlib import Path

# fhr R-group reverse/fill is gated by FHR_REVERSE at engine IMPORT time (engine/markush.py:_FHR_ON), so it
# MUST be set before chem_harness/engine is first imported. Default it ON here (matches the balanced-300 harness)
# unless the host explicitly overrode it. Same for the token cap. These are setdefault -> an explicit env wins.
os.environ.setdefault("FHR_REVERSE", "1")
os.environ.setdefault("MAX_TOKENS", "2000000")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (
    str(REPO),
    str(HERE),
    str(REPO / "tools_product"),
    str(REPO / "third_party" / "molecular_assembly_v2" / "src"),
    str(REPO / "third_party" / "molecular_assembly" / "src"),
):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("CHEM_REPO", str(REPO))

SERVER_NAME = "chem-extract"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2024-11-05"


def log(*a):
    print("[chem_mcp]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- tool implementations
# Each returns a plain str or a JSON-serializable object; the dispatcher wraps it as MCP text content.

def _rdkit():
    from rdkit import Chem, RDLogger
    RDLogger.DisableLog("rdApp.*")
    return Chem


def _esmiles_semantic_audit(smiles: str):
    """Report extension semantics without rewriting the immutable input string."""
    from tools.esmiles_audit import audit_esmiles
    return audit_esmiles(smiles)


def tool_verify_smiles(smiles: str, require_concrete: bool = False):
    """Objective structure, stereo, and Extended-SMILES semantic audit.

    Ordinary SMILES are verified with RDKit. Tagged Extended-SMILES additionally report unsupported
    labels/tags, open Markush slots, expansion, and whether the complete semantics resolve to a concrete
    molecule. ``require_concrete=true`` fails closed when only the wildcard body parses. The immutable
    input is never rewritten; callers must not mistake a body-only parse for full E-SMILES validity.
    """
    Chem = _rdkit()
    esmiles_audit = _esmiles_semantic_audit(smiles)
    verification_text = smiles or ""
    if esmiles_audit["present"] and esmiles_audit["concrete_valid"]:
        verification_text = esmiles_audit["expanded"]
    s = verification_text.split("<sep>", 1)[0].strip()
    m = Chem.MolFromSmiles(s) if s else None
    if m is None:
        return {
            "valid": False,
            "body_valid": False,
            "input": smiles,
            "reason": "rdkit could not parse",
            "esmiles_audit": esmiles_audit,
        }

    from rdkit.Chem import rdMolDescriptors
    from rdkit.Chem.Draw import rdMolDraw2D
    import render_v2

    Chem.AssignStereochemistry(m, cleanIt=True, force=True)
    centers = []
    for atom_idx, cip in Chem.FindMolChiralCenters(
            m, force=True, includeUnassigned=True, includeCIP=True,
            useLegacyImplementation=False):
        atom = m.GetAtomWithIdx(atom_idx)
        centers.append({
            "atom_index": atom_idx,
            "element": atom.GetSymbol(),
            "cip": cip,
            "assigned": cip in ("R", "S"),
            "chiral_tag": str(atom.GetChiralTag()),
        })

    double_bonds = []
    for info in Chem.FindPotentialStereo(m):
        if str(info.type) != "Bond_Double":
            continue
        bond = m.GetBondWithIdx(int(info.centeredOn))
        stereo = str(bond.GetStereo()).replace("STEREO", "")
        # RDKit may expose equivalent relative names after FindPotentialStereo.
        stereo = {"TRANS": "E", "CIS": "Z"}.get(stereo, stereo)
        if stereo in ("NONE", "ANY", ""):
            stereo = "UNSPECIFIED"
        double_bonds.append({
            "bond_index": bond.GetIdx(),
            "begin_atom_index": bond.GetBeginAtomIdx(),
            "end_atom_index": bond.GetEndAtomIdx(),
            "stereo": stereo,
            "assigned": stereo in ("E", "Z"),
            "stereo_atom_indices": list(bond.GetStereoAtoms()),
        })

    drawer = rdMolDraw2D.MolDraw2DSVG(420, 300)
    drawer.drawOptions().addAtomIndices = True
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, m)
    drawer.FinishDrawing()

    # Pi's image reader can inspect PNG directly, while SVG may be returned as text. Keep both a
    # clean and atom-indexed PNG so source-grounded verifiers can compare the candidate drawing
    # against the source crop instead of relying on formula/canonical text alone.
    render_dir = Path(tempfile.mkdtemp(prefix="chem-verify-render-"))
    plain_png = render_dir / "molecule.png"
    indexed_png = render_dir / "molecule.indexed.png"
    for target, indexed in ((plain_png, False), (indexed_png, True)):
        png_drawer = rdMolDraw2D.MolDraw2DCairo(600, 400)
        png_drawer.drawOptions().addAtomIndices = indexed
        rdMolDraw2D.PrepareAndDrawMolecule(png_drawer, m)
        png_drawer.FinishDrawing()
        target.write_bytes(png_drawer.GetDrawingText())

    tetra_assigned = sum(1 for x in centers if x["assigned"])
    alkene_assigned = sum(1 for x in double_bonds if x["assigned"])
    warnings = []
    if tetra_assigned < len(centers):
        warnings.append("potential tetrahedral stereocentre(s) are unassigned")
    if alkene_assigned < len(double_bonds):
        warnings.append("potential E/Z double bond(s) are unassigned")

    concrete_failure = bool(require_concrete and not esmiles_audit["concrete_valid"])
    return {
        "valid": not concrete_failure,
        "body_valid": True,
        "input": smiles,
        "verified_smiles": s,
        "rendering_scope": (
            "full_expanded" if esmiles_audit["present"] and esmiles_audit["concrete_valid"]
            else "body_only_partial" if esmiles_audit["present"]
            else "ordinary_smiles"
        ),
        "reason": (
            "Extended-SMILES did not resolve to a concrete supported molecule"
            if concrete_failure else None
        ),
        "esmiles_audit": esmiles_audit,
        "canonical_iso": Chem.MolToSmiles(m, isomericSmiles=True),
        "canonical_flat": Chem.MolToSmiles(m, isomericSmiles=False),
        "formula": rdMolDescriptors.CalcMolFormula(m),
        "n_atoms": m.GetNumAtoms(),
        "stereocenters": centers,
        "double_bond_stereo": double_bonds,
        "stereo_summary": {
            "tetrahedral_total": len(centers),
            "tetrahedral_assigned": tetra_assigned,
            "double_bond_total": len(double_bonds),
            "double_bond_assigned": alkene_assigned,
        },
        "stereo_warnings": warnings,
        "render_png_path": str(plain_png),
        "indexed_render_png_path": str(indexed_png),
        "svg": render_v2.svg(s),
        "indexed_svg": drawer.GetDrawingText(),
    }


def tool_render_reactions(v2_path: str, image_path: str = "", out_html: str = ""):
    """Render a V2 extraction to ONE self-contained HTML (reactants -> conditions -> products, yields,
    markush templates). Lets a human / the model VIEW the extraction. Reuses scripts/render_v2.py."""
    import html as _html
    import render_v2 as R
    v2 = json.load(open(v2_path))
    ex = v2.get("selected_examples") or []
    tpl = v2.get("reaction_templates") or []
    figb64 = None
    if image_path and os.path.exists(image_path):
        import base64
        figb64 = base64.b64encode(open(image_path, "rb").read()).decode()
    fig = ('<div class="fig"><img src="data:image/png;base64,%s"></div>' % figb64) if figb64 else ""
    doc = R.HTML % {
        "src": _html.escape(os.path.basename(v2_path)),
        "rclass": _html.escape(str(v2.get("reaction_class") or "-")),
        "nex": len(ex), "ntpl": len(tpl), "fig": fig,
        "tpl": "".join(R.rxn_row(i + 1, t, "T") for i, t in enumerate(tpl)) or '<div class="dim">(no templates)</div>',
        "ex": "".join(R.rxn_row(i + 1, e) for i, e in enumerate(ex)) or '<div class="dim">(no examples)</div>',
    }
    out = out_html or (os.path.splitext(v2_path)[0] + ".view.html")
    open(out, "w").write(doc)
    return {"html_path": out, "n_examples": len(ex), "n_templates": len(tpl)}


def _image_hashes(src):
    """Return content hashes used by the two supported cache layouts."""
    payload = src.read_bytes()
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "md5": hashlib.md5(payload).hexdigest(),  # compatibility with the existing OCSR_CACHE corpus
    }


def _legacy_parser_result_detections(value, expected_md5, image_size):
    """Adapt an immutable full parser-result document to the detection contract.

    Acceptance/reviewer exports store MolParser's complete response (``pages_dict``) rather
    than the compact detection list returned by ``/extract-mol``. Keep that source document
    byte-immutable, verify its declared image MD5, and adapt molecule records in memory. A
    molecule whose OCSR failed is retained with its bbox and no ``esmi`` so visual audit still
    sees the drawn occurrence instead of silently dropping it.
    """
    if not isinstance(value, dict) or not isinstance(value.get("pages_dict"), list):
        return None
    declared_md5 = value.get("md5")
    if not declared_md5:
        raise ValueError("full parser-result cache has no image md5")
    if str(declared_md5).lower() != expected_md5.lower():
        raise ValueError("full parser-result md5 does not match the input image")

    if image_size is None:
        raise ValueError("full parser-result cache requires a readable input image")
    width, height = image_size
    detections = []
    for page in value["pages_dict"]:
        if not isinstance(page, list):
            continue
        for item in page:
            if not isinstance(item, dict) or item.get("type") != "molecule":
                continue
            raw_bbox = item.get("bbox")
            if isinstance(raw_bbox, dict):
                raw_bbox = [raw_bbox.get(key) for key in ("x1", "y1", "x2", "y2")]
            if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
                continue
            try:
                bbox = [float(value) for value in raw_bbox]
            except (TypeError, ValueError):
                continue
            if max(bbox) <= 1.1:
                bbox = [bbox[0] * width, bbox[1] * height,
                        bbox[2] * width, bbox[3] * height]
            detection = {
                "bbox_index": len(detections),
                "bbox": [int(round(value)) for value in bbox],
                "score": item.get("conf", item.get("confidence")),
                "markush": bool(item.get("markush", item.get("is_markush", False))),
                "md5": expected_md5,
            }
            # Preserve the exact strings from the immutable parser result. The slim tool
            # response exposes only esmi; caption/smi remain available solely in raw provenance.
            for field in ("caption", "smi", "esmi"):
                if isinstance(item.get(field), str):
                    detection[field] = item[field]
            detections.append(detection)
    return detections


def _cache_detections(value, expected_hashes, image_size):
    """Read compact caches, hash-bound envelopes, or immutable full parser results."""
    cache_format = "detection_list"
    if isinstance(value, list):
        detections = value
    elif isinstance(value, dict) and isinstance(value.get("detections"), list):
        cache_hash = value.get("image_sha256")
        if cache_hash and cache_hash != expected_hashes["sha256"]:
            raise ValueError("cache image_sha256 does not match the input image")
        detections = value["detections"]
        cache_format = "detection_envelope"
    else:
        detections = _legacy_parser_result_detections(
            value, expected_hashes["md5"], image_size,
        )
        if detections is None:
            raise ValueError(
                "cache must be a MolParser detection list, {detections:[...]}, "
                "or a full parser result with pages_dict and md5"
            )
        cache_format = "full_parser_result"
    if not all(isinstance(item, dict) for item in detections):
        raise ValueError("cache contains a non-object detection")
    return detections, cache_format


def _read_molparser_cache(cache_path, expected_hashes, image_size):
    try:
        with cache_path.open(encoding="utf-8") as handle:
            return _cache_detections(json.load(handle), expected_hashes, image_size)
    except Exception as exc:
        log(f"ignoring unreadable MolParser cache {cache_path}: {exc}")
        return None


def _write_json_atomic(target, value):
    """Best-effort exact raw-cache write; never leave a truncated JSON file."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:
        log(f"could not persist MolParser cache {target}: {exc}")
        return False


def _call_live_molparser(image_path):
    """Single network boundary, kept separate so cache behavior is offline-testable."""
    from ocsr_client import molparser
    return molparser(str(image_path))


def _cached_molparser_with_provenance(image_path, force_live=False):
    """Load OCSR once, preferring immutable caches and calling MolParser only for a new image.

    Lookup order is ``<stem>.molparser.json`` -> sibling ``molparser.json`` ->
    ``$OCSR_CACHE/<md5(image)>.json`` -> live MolParser. A successful live call is atomically
    persisted to the stem-specific sibling, the conventional sibling when absent, and the
    optional global cache. Set ``OCSR_OFFLINE=1`` to forbid a network fallback.
    """
    src = Path(image_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"image not found: {image_path}")
    hashes = _image_hashes(src)
    try:
        from PIL import Image
        with Image.open(src) as image:
            image_size = image.size
    except Exception:
        # Compact detection-list caches do not need image dimensions. Legacy full parser
        # results do, because their bbox coordinates are normalized to the source image.
        image_size = None
    named_sibling = src.with_name(f"{src.stem}.molparser.json")
    conventional_sibling = src.parent / "molparser.json"
    global_cache = None
    if os.environ.get("OCSR_CACHE"):
        global_cache = Path(os.environ["OCSR_CACHE"]).expanduser().resolve() / f"{hashes['md5']}.json"

    if not force_live:
        candidates = [
            ("named_sibling_cache", named_sibling),
            ("sibling_cache", conventional_sibling),
        ]
        if global_cache is not None:
            candidates.append(("md5_cache", global_cache))
        for source, cache_path in candidates:
            if not cache_path.is_file():
                continue
            cached = _read_molparser_cache(cache_path, hashes, image_size)
            if cached is not None:
                detections, cache_format = cached
                return detections, {
                    "source": source,
                    "cache_hit": True,
                    "cache_path": str(cache_path),
                    "cache_format": cache_format,
                    "cache_key": hashes["md5"] if source == "md5_cache" else hashes["sha256"],
                    "image_sha256": hashes["sha256"],
                    "image_md5": hashes["md5"],
                    "persisted_paths": [],
                }

    if os.environ.get("OCSR_OFFLINE", "").lower() in ("1", "true", "yes"):
        raise RuntimeError(
            "MolParser cache miss while OCSR_OFFLINE=1. Provide a sibling molparser.json, "
            "<image-stem>.molparser.json, or $OCSR_CACHE/<md5>.json."
        )

    detections = _call_live_molparser(src) or []
    if not isinstance(detections, list) or not all(isinstance(item, dict) for item in detections):
        raise RuntimeError("live MolParser returned an invalid detection payload")
    persisted = []
    for cache_path in (named_sibling, global_cache):
        if cache_path is not None and _write_json_atomic(cache_path, detections):
            persisted.append(str(cache_path))
    if not conventional_sibling.exists() and _write_json_atomic(conventional_sibling, detections):
        persisted.append(str(conventional_sibling))
    return detections, {
        "source": "live_molparser",
        "cache_hit": False,
        "cache_path": str(named_sibling) if named_sibling.is_file() else None,
        "cache_format": "detection_list",
        "cache_key": hashes["sha256"],
        "image_sha256": hashes["sha256"],
        "image_md5": hashes["md5"],
        "persisted_paths": persisted,
    }


def _slim_detections(detections):
    # The raw cache remains byte-faithful and contains caption/smi/esmi. The extraction contract
    # exposes only E-SMILES so the agent cannot accidentally choose a different serialization.
    fields = ("bbox_index", "bbox", "esmi", "score", "markush", "md5")
    return [
        {key: detection.get(key) for key in fields if key in detection}
        for detection in detections
        if isinstance(detection, dict)
    ]


def tool_ocsr_detect(image_path: str, force_live: bool = False):
    """Read every drawn structure with MolParser.

    Reuses a sibling/global cache without network. For a genuinely new image it calls the
    configured MolParser endpoint directly (never through SOCKS), atomically saves the raw
    detections for later runs, and returns explicit cache/live provenance.
    """
    detections, provenance = _cached_molparser_with_provenance(image_path, force_live=force_live)
    slim = _slim_detections(detections)
    return {"n": len(slim), "detections": slim, "ocsr": provenance}


def _write_concrete_audit_crops(src: Path, detections, out_dir: Path):
    """Write one enlarged raw-figure crop per concrete detection for mandatory visual audit."""
    from PIL import Image
    image = Image.open(src).convert("RGB")
    crop_dir = out_dir / f"{src.stem}_audit_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for detection in detections:
        if not isinstance(detection, dict) or detection.get("markush") or not detection.get("bbox"):
            continue
        try:
            x0, y0, x1, y1 = [int(value) for value in detection["bbox"]]
        except (TypeError, ValueError):
            continue
        # Include nearby printed substituent/identifier text that may sit just outside MolParser's box.
        pad_x = max(18, int((x1 - x0) * 0.12))
        pad_y = max(18, int((y1 - y0) * 0.18))
        box = (max(0, x0 - pad_x), max(0, y0 - pad_y),
               min(image.width, x1 + pad_x), min(image.height, y1 + pad_y))
        crop = image.crop(box)
        scale = max(2, min(5, 900 // max(1, crop.width)))
        crop = crop.resize((crop.width * scale, crop.height * scale))
        index = detection.get("bbox_index")
        path = crop_dir / f"bbox_{int(index):03d}.png"
        crop.save(path)
        written.append({"bbox_index": index, "path": str(path)})
    return written


def tool_detect(image_path: str, out_dir: str = "", force_live: bool = False):
    """OCSR + VISUALIZE (the read-first entry point).

    Cache-first MolParser reads every structure, then this writes an annotated overview and an
    OCSR compare grid for the agent to OPEN. A cache miss calls live MolParser and persists the
    exact raw detections, so later extraction/retry runs do not repeat the request.
    """
    src = Path(image_path).expanduser().resolve()
    if not src.is_file():
        return {"error": f"image not found: {image_path}"}
    detections, provenance = _cached_molparser_with_provenance(src, force_live=force_live)
    od = Path(out_dir).expanduser().resolve() if out_dir else src.parent
    od.mkdir(parents=True, exist_ok=True)
    raw = od / f"{src.stem}_molparser.json"
    if provenance.get("cache_format") == "full_parser_result" and provenance.get("cache_path"):
        # Preserve the full parser response byte-for-byte as the raw provenance artifact.
        import shutil
        cache_path = Path(provenance["cache_path"])
        if cache_path != raw:
            shutil.copyfile(cache_path, raw)
    else:
        _write_json_atomic(raw, detections)
    overview = od / f"{src.stem}_overview.png"
    grid = od / f"{src.stem}_ocsr_grid.png"
    try:
        from ocsr_visualize import annotate_detections, render_detections_grid
        annotate_detections(str(src), detections, str(overview))
        render_detections_grid(detections, str(src), str(grid))
        audit_crops = _write_concrete_audit_crops(src, detections, od)
    except Exception as exc:                                    # visualization is best-effort; detections still returned
        log(f"visualize failed: {exc}")
        overview = grid = None
        audit_crops = []
    slim = _slim_detections(detections)
    n_concrete = sum(1 for detection in slim if not detection.get("markush"))
    return {
        "n": len(slim),
        "n_concrete": n_concrete,
        "n_markush": len(slim) - n_concrete,
        "detections": slim,
        "ocsr": provenance,
        "raw_molparser_json": str(raw),
        "overview_png": str(overview) if overview else None,
        "compare_grid_png": str(grid) if grid else None,
        "concrete_audit_crops": audit_crops,
        "hint": "OPEN overview_png and compare_grid_png, then OPEN EVERY concrete_audit_crops path. "
                "For each concrete molecule record ocsr_audit:{decision:'accepted|override',evidence:'what the "
                "raw crop explicitly shows'} in the plan. Assembly fails closed when a molecule carrying "
                "molparser_esmi lacks this audit. Never mutate the raw MolParser result.",
    }


def tool_assemble(plan=None, out_path: str = "", plan_path: str = ""):
    """ASSEMBLE the intermediate plan into strict V2 -- the DECOUPLE step. The agent produces an
    understanding-friendly plan (what it read: reactants/products/conditions/identifier/description per
    cell); this engine formats V2, preserves accepted MolParser E-SMILES verbatim, canonicalises only
    agent-built chemistry, stamps provenance, trusts MolParser reads
    for concrete cells, and recovers printed compound numbers -- so the agent NEVER has to hand-hold the
    V2 schema. plan = {selected_examples:[{reactants:[{smiles,identifier,bbox_index,amount}], products:[...],
    conditions:[[{role,text}]], label/identifier, description, markush_enumeration}], reaction_templates:[...]}.
    Offline. Pass exactly one of plan (an object or JSON string) and plan_path (a JSON file)."""
    from engine.v2 import assemble_and_validate
    if plan is not None and plan_path:
        raise ValueError("pass only one of plan and plan_path")
    if plan_path:
        p = json.load(open(plan_path, encoding="utf-8"))
    else:
        p = json.loads(plan) if isinstance(plan, str) else plan
    if not isinstance(p, dict):
        raise TypeError(f"plan must be an object, got {type(p).__name__}")
    v2, report = assemble_and_validate(p)
    out = Path(out_path) if out_path else Path(tempfile.mkdtemp(prefix="chem_asm_")) / "out.v2.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(v2, open(out, "w"), ensure_ascii=False, indent=1)
    ex = v2.get("selected_examples") or []
    inventory = []
    for section in ("reaction_templates", "selected_examples"):
        for reaction_i, reaction in enumerate(v2.get(section) or []):
            reaction_id = reaction.get("instance_id") or reaction.get("template_id") or str(reaction_i + 1)
            for role in ("reactants", "products"):
                for molecule_i, molecule in enumerate(reaction.get(role) or []):
                    if not isinstance(molecule, dict):
                        continue
                    source = molecule.get("_source") if isinstance(molecule.get("_source"), dict) else {}
                    inventory.append({
                        "key": f"{section}:{reaction_id}:{role}:{molecule_i}",
                        "section": section,
                        "reaction_id": reaction_id,
                        "role": role,
                        "ordinal": molecule_i,
                        "identifier": str(molecule.get("identifier") or "")[:300],
                        "E-SMILES": str(molecule.get("E-SMILES") or "")[:1200],
                        "bbox_index": source.get("bbox_index", molecule.get("bbox_index")),
                        "molparser_md5": source.get("molparser_md5", molecule.get("molparser_md5")),
                        "origin": source.get("origin") or "",
                    })
    candidate_bytes = out.read_bytes()
    return {"v2_path": str(out), "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "n_examples": len(ex), "n_templates": len(v2.get("reaction_templates") or []),
            "molecule_inventory": inventory[:120], "inventory_total": len(inventory),
            "inventory_truncated": len(inventory) > 120,
            "report": (report or "")[:1500], "v2": v2}


def tool_reverse_rgroup(template: str, concrete: str):
    """R-group REVERSE: markush template product E-SMILES + one concrete product -> {label: '*frag'}.
    Routes through engine.markush.reverse_r, the SAME fhr-aware path the extraction pipeline uses (fhr
    step0_scaffold_map peel when FHR_REVERSE=1, with MarkushMapMulti/RDKit-RGD union fallback)."""
    from engine.markush import reverse_r
    r = reverse_r(template, concrete)
    return {"template": template, "concrete": concrete, "r_map": r,
            "fhr": os.environ.get("FHR_REVERSE") == "1"}


def tool_fill_rgroup(template: str, r_map):
    """R-group FILL: markush template (with <a>idx:label</a> tags) + an R-map -> the concrete molecule.
    Routes through engine.markush.forward_fill, the SAME fhr-aware path the pipeline uses (fhr
    assemble_fragments when FHR_REVERSE=1 -> preserves E/Z + tetrahedral stereo, with molzip fallback).
    r_map may be a single {label: '*frag'} object OR a JSON LIST of such maps (batch, one product per cell)."""
    from engine.markush import forward_fill
    parsed = json.loads(r_map) if isinstance(r_map, str) else r_map
    fhr = os.environ.get("FHR_REVERSE") == "1"
    if isinstance(parsed, list):                        # batch: one filled product per cell
        return {"template": template, "filled": [forward_fill(template, m) for m in parsed], "fhr": fhr}
    return {"template": template, "filled": forward_fill(template, parsed), "fhr": fhr}


def _reverse_assemble_row_failures(rows):
    """Validate fhr rows independently of its aggregate status.

    A partial assembly may be useful diagnostic output, but it is never a
    concrete reactant.  In particular, isotope-labelled ``*`` atoms are still
    atomic-number-zero connection ports and must not pass as finished SMILES.
    """
    Chem = _rdkit()
    failed = []
    for index, row in enumerate(rows or []):
        reasons = []
        smiles = (row.get("smiles") or "").split("<sep>", 1)[0].strip()
        mol = Chem.MolFromSmiles(smiles) if smiles else None
        dummies = []
        if not smiles:
            reasons.append("empty_smiles")
        elif mol is None:
            reasons.append("smiles_parse_failed")
        else:
            dummies = [
                {
                    "atom_index": atom.GetIdx(),
                    "isotope": atom.GetIsotope(),
                    "atom_map": atom.GetAtomMapNum(),
                }
                for atom in mol.GetAtoms()
                if atom.GetAtomicNum() == 0
            ]
            if dummies:
                reasons.append("unconnected_dummy_atoms")
        if row.get("success") is not True:
            reasons.append("fhr_row_not_successful")
        if row.get("partial"):
            reasons.append("partial_assembly")
        if row.get("missing_labels"):
            reasons.append("missing_labels")
        if reasons:
            failed.append({
                "index": index,
                "reasons": list(dict.fromkeys(reasons)),
                "dummy_atoms": dummies,
                "missing_labels": list(row.get("missing_labels") or []),
                "messages": list(row.get("messages") or []),
            })
    return failed


def tool_reverse_assemble(target_product: str, product_template: str, reactant_templates):
    """REACTANT DERIVATION (high-level fhr): from ONE concrete product + its markush product template +
    the reactant markush templates, derive EVERY reactant's CONCRETE SMILES in a single call. This is the
    correct fhr entry (`chemistry.reverse_assemble`) -- it peels the product R-groups and fills each
    reactant template, deriving organometallic reactants (e.g. an alkylzinc `Br[Zn]C1CCC1`) that the
    per-cell reverse_rgroup+fill_rgroup path under-peels to None. For a scope grid: pass the top-scheme
    product template + the reactant templates + one cell's concrete product; get that cell's concrete
    reactants back (aligned with reactant_templates). reactant_templates may be a JSON list or an
    already-parsed list. Prefer molecular_assembly_v2 (better aryl ring-substituent handling). Offline."""
    from engine.markush import reverse_assemble_reactants
    rts = json.loads(reactant_templates) if isinstance(reactant_templates, str) else reactant_templates
    if not isinstance(rts, list):
        return {"error": "reactant_templates must be a JSON list of markush templates"}
    details = reverse_assemble_reactants(
        target_product,
        product_template,
        rts,
        return_details=True,
    )
    if details is None:
        return {"ok": False, "reactants": None,
                "reason": "fhr reverse_assemble unavailable or the product could not be peeled"}
    rows = details.get("reactants") or []
    failed = _reverse_assemble_row_failures(rows)
    if len(rows) != len(rts):
        failed.append({
            "index": None,
            "reasons": ["reactant_count_mismatch"],
            "expected": len(rts),
            "actual": len(rows),
        })
    ok = details.get("ok") is True and not failed
    reason = (
        ""
        if ok
        else "fhr reverse assembly is incomplete; inspect failed_reactants and diagnostics"
    )
    return {
        "ok": ok,
        "n": len(rows),
        "reactants": rows,
        "failed_reactants": failed,
        "reason": reason,
        "fhr": {
            key: details.get(key)
            for key in (
                "ok", "peel_ok", "roundtrip_ok", "roundtrip_note",
                "match_index", "match_source", "notes",
            )
        },
    }


# --------------------------------------------------------------------------- tool registry (name -> (fn, schema))
def _S(**props):
    return {"type": "object", "properties": props}

TOOLS = {
    "ocsr_detect": (tool_ocsr_detect,
        "Cache-first MolParser OCSR: reuse sibling/global raw detections, or call the live direct endpoint "
        "for a genuinely new image and persist the result. Returns bbox + verbatim esmi and provenance; caption/smi stay only in the raw cache.",
        {"type": "object", "required": ["image_path"], "properties": {
            "image_path": {"type": "string"},
            "force_live": {"type": "boolean", "default": False,
                           "description": "ignore valid caches and call MolParser again; use only when explicitly requested"}}}),
    "detect": (tool_detect,
        "READ-FIRST entry point: cache-first MolParser + visual evidence. Reuses sibling/$OCSR_CACHE; on "
        "a new image calls the direct MolParser endpoint and saves raw detections. Writes an overview PNG "
        "and OCSR compare grid to OPEN. Returns detections, raw cache path, and cache/live provenance.",
        {"type": "object", "required": ["image_path"], "properties": {
            "image_path": {"type": "string"}, "out_dir": {"type": "string", "default": ""},
            "force_live": {"type": "boolean", "default": False,
                           "description": "ignore valid caches and call MolParser again; use only when explicitly requested"}}}),
    "assemble": (tool_assemble,
        "DECOUPLE step: assemble your intermediate plan into strict V2 JSON. You produce what you READ "
        "(reactants/products/conditions/identifier/description per cell); this engine formats V2, "
        "preserves accepted MolParser esmi verbatim, canonicalises agent-built chemistry, trusts drawn cells, and recovers printed numbers. "
        "You never hand-hold the V2 schema. Offline.",
        {"type": "object", "oneOf": [{"required": ["plan"]}, {"required": ["plan_path"]}], "properties": {
            "plan": {"type": "object", "description": "intermediate plan {selected_examples, reaction_templates}"},
            "plan_path": {"type": "string", "description": "path to an intermediate-plan JSON file"},
            "out_path": {"type": "string", "default": ""}}}),
    "verify_smiles": (tool_verify_smiles,
        "Audit ordinary or Extended-SMILES: RDKit validity, canonical forms, CIP R/S, double-bond E/Z, "
        "indexed SVG, extension tags, supported/unsupported abbreviations, and unresolved dummies. "
        "Set require_concrete=true for catalysts and other concrete chemistry: a body-only wildcard "
        "parse then fails closed instead of pretending the full Extended-SMILES was validated. Offline.",
        {"type": "object", "required": ["smiles"], "properties": {
            "smiles": {"type": "string"},
            "require_concrete": {"type": "boolean", "default": False},
        }}),
    "render_reactions": (tool_render_reactions,
        "Render a V2 extraction to one self-contained HTML (reactions + yields + templates). Offline.",
        {"type": "object", "required": ["v2_path"], "properties": {
            "v2_path": {"type": "string"}, "image_path": {"type": "string", "default": ""},
            "out_html": {"type": "string", "default": ""}}}),
    "reverse_rgroup": (tool_reverse_rgroup,
        "R-group REVERSE: markush template + one concrete product -> the R substituents. Offline.",
        {"type": "object", "required": ["template", "concrete"], "properties": {
            "template": {"type": "string"}, "concrete": {"type": "string"}}}),
    "fill_rgroup": (tool_fill_rgroup,
        "R-group FILL: markush template + R-map (JSON of Rlabel->SMILES) -> concrete molecule. Offline.",
        {"type": "object", "required": ["template", "r_map"], "properties": {
            "template": {"type": "string"}, "r_map": {"type": "string", "description": "JSON object or string"}}}),
    "reverse_assemble": (tool_reverse_assemble,
        "REACTANT DERIVATION (high-level fhr): concrete product + markush product template + reactant "
        "templates -> every reactant's concrete SMILES in ONE call. Derives organometallic reactants "
        "(alkylzinc etc.) the per-cell reverse+fill path under-peels to None. Prefer this for a scope "
        "grid's undrawn reactants. Offline.",
        {"type": "object", "required": ["target_product", "product_template", "reactant_templates"],
         "properties": {
            "target_product": {"type": "string", "description": "one cell's concrete product SMILES"},
            "product_template": {"type": "string", "description": "markush product template E-SMILES (with <sep> tags)"},
            "reactant_templates": {"type": "string",
                                   "description": "JSON list of reactant markush templates (E-SMILES)"}}}),
}


# --------------------------------------------------------------------------- MCP stdio JSON-RPC loop
def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}

def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def _tools_list():
    return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (f, d, s) in TOOLS.items()]}

def _call_tool(name, arguments):
    if name not in TOOLS:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    fn = TOOLS[name][0]
    try:
        out = fn(**(arguments or {}))
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        log("tool error:", name, repr(e), "\n", traceback.format_exc())
        return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}

def handle(msg):
    """Return a response dict, or None for notifications (no reply)."""
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return _result(id_, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None                                            # notifications: no response
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, _tools_list())
    if method == "tools/call":
        return _result(id_, _call_tool(params.get("name"), params.get("arguments")))
    if id_ is None:
        return None                                            # unknown notification -> ignore
    return _error(id_, -32601, f"method not found: {method}")

def main():
    log(f"{SERVER_NAME} v{SERVER_VERSION} ready ({len(TOOLS)} tools):", ", ".join(TOOLS))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            log("bad JSON line:", repr(line[:200]), e)
            continue
        try:
            resp = handle(msg)
        except Exception as e:
            log("handler crash:", repr(e), "\n", traceback.format_exc())
            resp = _error(msg.get("id"), -32603, f"internal error: {e}")
        if resp is not None:
            _send(resp)
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
