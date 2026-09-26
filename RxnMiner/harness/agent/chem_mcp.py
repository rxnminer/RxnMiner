#!/usr/bin/env python3
"""chem_mcp -- a self-contained MCP (Model Context Protocol) stdio server that exposes the chemistry
engine (OCSR, reaction extraction, R-group reverse/fill, SMILES self-verify, V2 rendering) as tools any
MCP host (opencode, Goose, Claude Code, ...) can drive.

WHY hand-rolled (no `mcp` SDK): agent/.venv is py3.9 but the `mcp` package needs py3.10+. This module speaks
the MCP stdio wire protocol directly with the stdlib (newline-delimited JSON-RPC 2.0), so it runs in the SAME
interpreter that already has rdkit + our whole codebase -- zero new deps, portable to the release venv too.

RUN (opencode / any MCP host spawns this over stdio):
    <repo>/agent/.venv/bin/python <repo>/agent/chem_mcp.py

The heavy tools (extract_reactions, ocsr_detect) need the usual egress the batch harness needs -- set these
in the ENV the host launches us with:
    VERTEX_CREDS=<service-account.json>  http_proxy=https_proxy=grpc_proxy=http://127.0.0.1:17897   (Gemini)
The offline tools (verify_smiles, render_reactions, reverse_rgroup, fill_rgroup) need only rdkit.

PROTOCOL NOTE: stdout is the JSON-RPC channel -- NOTHING else may be written there. All logs go to stderr.
"""
import sys, os, json, tempfile, shutil, traceback
from pathlib import Path

# fhr R-group reverse/fill is gated by FHR_REVERSE at engine IMPORT time (engine/markush.py:_FHR_ON), so it
# MUST be set before chem_harness/engine is first imported. Default it ON here (matches the balanced-300 harness)
# unless the host explicitly overrode it. Same for the token cap. These are setdefault -> an explicit env wins.
os.environ.setdefault("FHR_REVERSE", "1")
os.environ.setdefault("MAX_TOKENS", "2000000")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO / "scripts"), str(REPO / "skills" / "chem_extract")):
    if p not in sys.path:                         # so chem_harness / render_v2 / engine.markush resolve
        sys.path.insert(0, p)

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


def tool_verify_smiles(smiles: str):
    """Objective self-verify (the sandbox-style check that REPLACES deterministic 'is this right' rules):
    parse a SMILES with rdkit, report validity + the canonical isomeric form + formula + an SVG so the model
    can SEE what it wrote and compare to the drawing."""
    Chem = _rdkit()
    s = (smiles or "").split("<sep>")[0].strip()
    m = Chem.MolFromSmiles(s) if s else None
    if m is None:
        return {"valid": False, "input": smiles, "reason": "rdkit could not parse"}
    from rdkit.Chem import rdMolDescriptors
    import render_v2
    return {
        "valid": True,
        "input": smiles,
        "canonical_iso": Chem.MolToSmiles(m, isomericSmiles=True),
        "canonical_flat": Chem.MolToSmiles(m, isomericSmiles=False),
        "formula": rdMolDescriptors.CalcMolFormula(m),
        "n_atoms": m.GetNumAtoms(),
        "svg": render_v2.svg(s),
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


def _cached_molparser_with_provenance(image_path):
    """OCSR read with a co-located cache. MolParser is deterministic per image, so a benchmark figure's
    detections can be precomputed once and reused every iteration instead of re-hitting the network.
    Lookup order: a sibling `molparser.json` next to the image (the "figure ships with its OCSR result"
    layout) -> `$OCSR_CACHE/<md5(image)>.json` -> the live MolParser service. Read-only (never writes the
    sibling); precompute the caches with a build script."""
    from tools import molparser
    src = Path(image_path).resolve()
    sib = src.parent / "molparser.json"
    if sib.exists():
        try:
            return json.load(open(sib)), {
                "source": "sibling_cache",
                "cache_path": str(sib),
            }
        except Exception as e:
            log(f"molparser.json unreadable ({e}); falling back to live")
    cdir = os.environ.get("OCSR_CACHE")
    if cdir and src.exists():
        import hashlib
        cpath = Path(cdir) / (hashlib.md5(src.read_bytes()).hexdigest() + ".json")
        if cpath.exists():
            try:
                return json.load(open(cpath)), {
                    "source": "md5_cache",
                    "cache_key": cpath.stem,
                    "cache_path": str(cpath),
                }
            except Exception:
                pass
    return molparser(str(src)), {"source": "live_molparser"}


def _cached_molparser(image_path):
    """Backward-compatible value-only wrapper used by older callers."""
    return _cached_molparser_with_provenance(image_path)[0]


def tool_ocsr_detect(image_path: str):
    """Run OCSR (MolParser, remote CN-IP service) on a figure -> every detected structure with its bbox,
    caption (faithful E-SMILES) and smi (canonical/filled). Uses a co-located `molparser.json` / $OCSR_CACHE
    if present (no network), else calls the live OCSR endpoint."""
    dets = _cached_molparser(image_path)
    # trim to the fields a caller actually needs
    out = []
    for d in (dets or []):
        if not isinstance(d, dict):
            continue
        out.append({k: d.get(k) for k in ("bbox_index", "bbox", "caption", "smi", "score", "markush") if k in d})
    return {"n": len(out), "detections": out}


def tool_detect(image_path: str, out_dir: str = ""):
    """OCSR + VISUALIZE (the read-first entry point): run MolParser on the figure, then write TWO images
    the agent should OPEN and LOOK at -- an annotated overview (every drawn structure boxed with its
    bbox_index + a confidence colour: green>=0.9 trustworthy / amber-red verify) and an OCSR compare grid
    (per detection: drawn crop | caption-render | smi-render). Returns the detections (slim) + both PNG
    paths. Workflow: open overview_png, then USE each concrete detection's smi VERBATIM (markush=false ->
    a fully-read structure, do NOT re-type it) and reverse/fill the markush=true templates. Uses a co-located
    `molparser.json` / $OCSR_CACHE if present (no network), else the live OCSR service."""
    src = Path(image_path).resolve()
    if not src.exists():
        return {"error": f"image not found: {image_path}"}
    dets, provenance = _cached_molparser_with_provenance(str(src))
    dets = dets or []
    od = Path(out_dir) if out_dir else src.parent
    od.mkdir(parents=True, exist_ok=True)
    overview = od / f"{src.stem}_overview.png"
    grid = od / f"{src.stem}_ocsr_grid.png"
    raw = od / f"{src.stem}_molparser.json"
    # Preserve the exact MolParser/cache value before any UI-facing field trimming.
    raw.write_text(json.dumps(dets, ensure_ascii=False, indent=1))
    try:
        from chem_harness import annotate_detections, render_detections_grid
        annotate_detections(str(src), dets, str(overview))
        render_detections_grid(dets, str(src), str(grid))
    except Exception as e:                                       # visualization is best-effort; detections still returned
        log(f"visualize failed: {e}")
        overview = grid = None
    slim = [{k: d.get(k) for k in ("bbox_index", "bbox", "caption", "smi", "score", "markush") if k in d}
            for d in dets if isinstance(d, dict)]
    n_concrete = sum(1 for d in slim if not d.get("markush"))
    return {"n": len(slim), "n_concrete": n_concrete, "n_markush": len(slim) - n_concrete,
            "detections": slim,
            "raw_molparser_json": str(raw),
            "molparser_provenance": provenance,
            "overview_png": str(overview) if overview else None,
            "compare_grid_png": str(grid) if grid else None,
            "hint": "OPEN overview_png first. Each markush=false detection is a fully-read structure -- use its "
                    "smi VERBATIM (do NOT re-type). Reverse/fill the markush=true templates. Read printed "
                    "compound numbers + conditions off the figure."}


def tool_assemble(plan, out_path: str = ""):
    """ASSEMBLE the intermediate plan into strict V2 -- the DECOUPLE step. The agent produces an
    understanding-friendly plan (what it read: reactants/products/conditions/identifier/description per
    cell); this engine formats V2, canonicalises every SMILES, stamps provenance, trusts MolParser reads
    for concrete cells, and recovers printed compound numbers -- so the agent NEVER has to hand-hold the
    V2 schema. plan = {selected_examples:[{reactants:[{smiles,identifier,bbox_index,amount}], products:[...],
    conditions:[[{role,text}]], label/identifier, description, markush_enumeration}], reaction_templates:[...]}.
    Offline."""
    from engine.v2 import assemble_and_validate
    p = json.loads(plan) if isinstance(plan, str) else plan
    if not isinstance(p, dict):
        return {"error": f"plan must be an object, got {type(p).__name__}"}
    v2, report = assemble_and_validate(p)
    out = Path(out_path) if out_path else Path(tempfile.mkdtemp(prefix="chem_asm_")) / "out.v2.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(v2, open(out, "w"), ensure_ascii=False, indent=1)
    ex = v2.get("selected_examples") or []
    return {"v2_path": str(out), "n_examples": len(ex),
            "n_templates": len(v2.get("reaction_templates") or []),
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
    rows = reverse_assemble_reactants(target_product, product_template, rts)
    if rows is None:
        return {"ok": False, "reactants": None,
                "reason": "fhr reverse_assemble unavailable or the product could not be peeled"}
    return {"ok": any(r.get("smiles") for r in rows), "n": len(rows), "reactants": rows}


def tool_extract_reactions(image_path: str, provider: str = "gemini-vertex", model: str = "",
                           no_ocsr: bool = False, max_tokens: int = 2_000_000, max_iters: int = 30):
    """THE core tool: extract EVERY reaction from ONE figure into V2 JSON via the chem_harness agent loop
    (OCSR -> R reverse/fill -> emit). Stages the image the way scripts/rxncaption_extract does, runs the loop,
    returns the V2 + a short summary. NEEDS Gemini egress (VERTEX_CREDS + proxy 17897) unless a local provider
    is used. check_full is already stripped by default (net-negative)."""
    import chem_harness as H
    src = Path(image_path).resolve()
    if not src.exists():
        return {"error": f"image not found: {image_path}"}
    work = Path(tempfile.mkdtemp(prefix="chem_mcp_"))
    shutil.copyfile(src, work / "0.png")
    if not no_ocsr:
        try:
            from tools import molparser
            (work / "0_molparser.json").write_text(json.dumps(molparser(str(work / "0.png")), ensure_ascii=False))
        except Exception as e:                                   # OCSR optional -> pure-vision fallback
            log(f"ocsr failed ({e}); continuing pure-vision")
            no_ocsr = True
    out_v2 = work / "out.v2.json"
    res = H.run(str(work), 0, str(out_v2), provider=provider, model_name=(model or None),
                use_detections=(not no_ocsr), max_tokens=int(max_tokens), max_iters=int(max_iters))
    if not out_v2.exists():
        return {"error": "extraction produced no V2 output", "work_dir": str(work)}
    v2 = json.load(open(out_v2))
    ex = v2.get("selected_examples") or []
    def _prod(e):
        return [ (p.get("E-SMILES") if isinstance(p, dict) else p) for p in (e.get("products") or []) ]
    with_prod = sum(1 for e in ex if any(_prod(e)))
    return {
        "v2_path": str(out_v2), "image_copy": str(work / "0.png"),
        "n_examples": len(ex), "n_templates": len(v2.get("reaction_templates") or []),
        "examples_with_product": with_prod,
        "reaction_class": v2.get("reaction_class"),
        "valid": res.get("valid"), "iters": res.get("iters"),
        "tokens_in": res.get("tokens_in"), "tokens_out": res.get("tokens_out"),
        "tokens_total": res.get("tokens"), "tokens_cached": res.get("tokens_cached"),
        "v2": v2,
    }


# --------------------------------------------------------------------------- tool registry (name -> (fn, schema))
def _S(**props):
    return {"type": "object", "properties": props}

TOOLS = {
    "extract_reactions": (tool_extract_reactions,
        "Extract EVERY reaction from one chemistry figure into structured V2 JSON (the full agent loop: "
        "OCSR -> R-group reverse/fill -> emit). Needs Gemini egress unless a local provider is set.",
        {"type": "object", "required": ["image_path"], "properties": {
            "image_path": {"type": "string", "description": "path to the figure image (png/jpg)"},
            "provider": {"type": "string", "default": "gemini-vertex",
                         "description": "gemini-vertex | byo-gemini | local-qwen | geek | ..."},
            "model": {"type": "string", "description": "optional model override; empty = provider default"},
            "no_ocsr": {"type": "boolean", "default": False, "description": "skip OCSR, pure model vision"},
            "max_tokens": {"type": "integer", "default": 2000000},
            "max_iters": {"type": "integer", "default": 30}}}),
    "ocsr_detect": (tool_ocsr_detect,
        "Run OCSR (MolParser) on a figure: every drawn structure with bbox + caption + smi. Needs OCSR network.",
        {"type": "object", "required": ["image_path"], "properties": {
            "image_path": {"type": "string"}}}),
    "detect": (tool_detect,
        "READ-FIRST entry point: OCSR + VISUALIZE. Runs MolParser and writes an annotated overview PNG "
        "(boxed structures + bbox_index + confidence colour) and an OCSR compare grid PNG for you to OPEN "
        "and look at. Returns slim detections + both PNG paths. Use each markush=false detection's smi "
        "verbatim; reverse/fill the markush=true templates. Needs OCSR network.",
        {"type": "object", "required": ["image_path"], "properties": {
            "image_path": {"type": "string"}, "out_dir": {"type": "string", "default": ""}}}),
    "assemble": (tool_assemble,
        "DECOUPLE step: assemble your intermediate plan into strict V2 JSON. You produce what you READ "
        "(reactants/products/conditions/identifier/description per cell); this engine formats V2, "
        "canonicalises SMILES, trusts MolParser reads for concrete cells, and recovers printed numbers. "
        "You never hand-hold the V2 schema. Offline.",
        {"type": "object", "required": ["plan"], "properties": {
            "plan": {"type": "object", "description": "intermediate plan {selected_examples, reaction_templates}"},
            "out_path": {"type": "string", "default": ""}}}),
    "verify_smiles": (tool_verify_smiles,
        "Self-verify a SMILES with rdkit: validity, canonical isomeric/flat form, formula, and an SVG to eyeball. "
        "Offline. Use this to CHECK a structure instead of trusting a deterministic rule.",
        {"type": "object", "required": ["smiles"], "properties": {"smiles": {"type": "string"}}}),
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
