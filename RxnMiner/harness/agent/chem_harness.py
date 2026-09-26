"""chem_harness — an AGENTIC tool-use loop around Gemini 3.1 Pro (Vertex) that drives the
chem_extract_skills tools to extract ONE figure into the V2 schema autonomously.

Unlike the staged one-shot `pipeline.py` (perceive -> caption -> single structured call),
this is a real harness: Gemini sees each tool's feedback (including cropped images) and keeps
reasoning until it produces a VALID V2 or hits a cap. The hard-won checks that a one-shot run
misses are exposed as tools and demanded by the system prompt:

  * crop  ........ zoom any structure to verify OCSR (catches mis-read wedges/regiochem)
  * opsin ........ IUPAC name -> structure (ground truth for stereochemistry)
  * cip  ......... CIP labels of a SMILES (compare drawn stereo to the named stereo)
  * canon/retro/roundtrip/cond/abbrev/graph .... the chem_extract_skills verbs
  * emit_full .... assemble + VALIDATE the plan into V2 (the gate; iterate until VALID)

Caps: --max-iters (tool-loop turns) and --max-tokens (summed Vertex usage). Both hard-stop.

The decisive lesson baked into SYSTEM: OCSR (MolParser) routinely draws the WRONG ENANTIOMER
of fused/cyclopropane systems. Whenever the caption or paper text names a compound with an
IUPAC stereo-descriptor, the agent MUST opsin() that name and cip()-compare; if it is the
enantiomer of the OCSR reading, flip it. (This is exactly what fixed compounds 7/8/13/14 in
paper 50.)

Run (real):
  python agent/chem_harness.py PDF_OA_50/50__10.1021_acs.oprd.4c00527 5 \
         --out /tmp/fig5.v2.json --max-iters 30 --max-tokens 400000
Run (plumbing self-test, no API / no creds):
  python agent/chem_harness.py --self-test
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tempfile, time, urllib.parse, urllib.request, uuid
from pathlib import Path

from sandbox_fs import SandboxFS   # High-Precision mode: a host workspace dir the agent owns

REPO   = Path(__file__).resolve().parent.parent
# Credentials: VERTEX_CREDS is ours; GOOGLE_APPLICATION_CREDENTIALS is the Google-standard name the README
# tells an outside user to set. Accept both so a stranger's documented setup actually reaches the model.
CREDS  = Path(os.environ.get("VERTEX_CREDS") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
              or (REPO / "agent" / ".creds" / "uniparser-new.json"))


def _default_project():
    """Vertex project: explicit env wins, else read project_id out of the service-account JSON, else ours.
    Hardcoding our project made every outside user's run fail with a permission error on OUR project."""
    for k in ("GOOGLE_CLOUD_PROJECT", "VERTEX_PROJECT", "GCLOUD_PROJECT"):
        v = os.environ.get(k)
        if v:
            return v
    try:
        with open(CREDS) as fh:
            pid = json.load(fh).get("project_id")
        if pid:
            return pid
    except Exception:
        pass
    return "uniparser-new"


PROJECT, LOCATION, MODEL = _default_project(), os.environ.get("VERTEX_LOCATION", "global"), "gemini-3.1-pro-preview"
MODEL_RETRIES = 3                        # retry the main agent model call on a TRANSIENT network/quota error
_MODEL_RETRY_SIGS = ("Max retries exceeded", "HTTPSConnectionPool", "Connection aborted", "Connection reset",
                     "Failed to establish a new connection", "Read timed out", "timed out", "ServiceUnavailable",
                     "503", "502", "ResourceExhausted", "429", "Remote end closed", "RemoteDisconnected")
SKILL  = Path(os.environ.get("SKILL_DIR") or (REPO / "skills" / "chem_extract_skills"))   # portable (was hardcoded local)
PY     = os.environ.get("HARNESS_PY") or sys.executable   # the interpreter running the harness (has the deps); portable across machines

# ----------------------------------------------------------------------------- tool impls
# Each returns {"result": <json-able>, "image": <png path or None>}. Chem ops shell out to the
# skill's tool.py so the harness stays thin and reuses every verb verbatim.
def _sh(args, timeout=120):
    p = subprocess.run([PY, str(SKILL / "tool.py"), *map(str, args)],
                       capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "").strip()
    try:    return json.loads(out)
    except Exception: return {"stdout": out, "stderr": (p.stderr or "")[-800:]}

def t_read_detections(paper_dir, figure_idx, **_):
    f = Path(paper_dir) / f"{figure_idx}_molparser.json"
    if not f.exists():
        return {"result": "no molparser file (OCSR not run on this figure)"}
    out = {"result": json.load(open(f))}
    grid = Path(paper_dir) / f"{figure_idx}_ocsr_grid.png"        # OCSR COMPARE GRID: figure-crop | OCSR-render per detection
    ov = Path(paper_dir) / f"{figure_idx}_annotated.png"          # fallback: re-show the labeled overview
    if grid.exists():
        out["image"] = str(grid)
        out["caption"] = ("OCSR COMPARE GRID attached: per detection, three panes — DRAWN (figure crop) | OCSR "
                          "caption-render | OCSR smi-render. Compare BOTH renders to the drawing; use whichever "
                          "matches (header flags 'caption != smi' when they disagree — common on stereo/markush). "
                          "If neither matches, OCSR misread it — fix the SMILES. Crop provided — do NOT re-crop.")
    elif ov.exists():
        out["image"] = str(ov)
    return out

def t_read_paper_text(paper_dir, query="", window=1200, **_):
    txts = list(Path(paper_dir).glob("*_paper_text.txt"))
    if not txts: return {"result": "no paper text"}
    s = txts[0].read_text(errors="ignore")
    if not query: return {"result": s[:window]}
    i = s.lower().find(query.lower())
    return {"result": (s[max(0, i-120): i+window] if i >= 0 else f"'{query}' not found")}

def t_crop(paper_dir, figure_idx, bbox, scale=4, _sbx=None, **_):
    # In High-Precision mode, crops live in the agent's workspace (re-viewable via look + path-referenced).
    # In Fast mode they go in the job's OWN paper_dir (per-job, auto-cleaned) — NOT a shared /tmp path, which
    # collided across concurrent jobs (figure_idx is always 0 and ms%100000 wraps -> two jobs cropping at the
    # same ms got each other's image). uuid4 guarantees a unique name within the job too.
    tag = uuid.uuid4().hex[:10]
    out = (_sbx.abspath(f"crop_{figure_idx}_{tag}.png") if _sbx
           else str(Path(paper_dir) / f"crop_{figure_idx}_{tag}.png"))
    r = _sh(["crop", str(Path(paper_dir)/f"{figure_idx}.png"),
             "--bbox", *bbox, "--scale", scale, "--out", out])
    if os.path.exists(out):
        _cap_image_px(out)          # crops accumulate in the stateful Vertex chat; on big figures (49-2/48-2)
                                    # the resent payload crosses Vertex's request limit -> 400. A single zoomed
                                    # structure at <=CROP_MAX_PX is plenty to verify wedges/regiochemistry.
    return {"result": r, "image": out if os.path.exists(out) else None}


CROP_MAX_PX = int(os.environ.get("CROP_MAX_PX", "1280"))   # longest-side cap for a crop image (env-tunable)


def _cap_image_px(path, max_px=None):
    """Downscale an image in place so its longest side <= max_px. Best-effort: any failure leaves the file
    untouched (the run still works, just with the larger image). Keeps the Vertex request from bloating."""
    cap = max_px or CROP_MAX_PX
    try:
        from PIL import Image
        im = Image.open(path)
        w, h = im.size
        if max(w, h) <= cap:
            return
        s = cap / float(max(w, h))
        im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS).save(path)
    except Exception:
        pass

def t_canon(smiles, **_):       return {"result": _sh(["canon", smiles])}
def t_cip(smiles, **_):
    code = ("import sys;from rdkit import Chem;m=Chem.MolFromSmiles(sys.argv[1]);"
            "Chem.AssignStereochemistry(m,cleanIt=True,force=True);"
            "print(__import__('json').dumps(sorted(f'{a.GetIdx()}:'+a.GetPropsAsDict().get('_CIPCode','?') "
            "for a in m.GetAtoms() if a.HasProp('_CIPCode'))))")
    p = subprocess.run([PY, "-c", code, smiles], capture_output=True, text=True)
    return {"result": (p.stdout.strip() or p.stderr[-300:])}

def t_opsin(name, **_):
    """IUPAC name -> canonical SMILES (Cambridge OPSIN web service). The stereo ground truth."""
    try:
        url = f"https://opsin.ch.cam.ac.uk/opsin/{urllib.parse.quote(name)}.smi"
        smi = urllib.request.urlopen(url, timeout=15).read().decode().strip()
        return {"result": (_sh(["canon", smi]) if smi else "OPSIN: no result")}
    except Exception as e:
        return {"result": f"OPSIN error: {e}"}

def t_retro(name, product, **_):     return {"result": _sh(["retro", name, product])}
def t_roundtrip(name, product, reactants, **_):
    return {"result": _sh(["roundtrip", name, "--product", product, "--reactants", *reactants])}
def t_reverse(template, concrete, **_):  return {"result": _sh(["reverse", template, concrete])}   # MarkushMap R-extraction


def t_fill(template, r_json, **_):
    """MarkushMap R-group fill. BATCH-CAPABLE:
      - r_json = ONE map {"R":"*OC"}  -> one filled product.
      - r_json = a JSON LIST of maps [{"R":"*OC"},{"R":"*C"},...] -> a LIST of filled products,
        one per cell (fill a whole scope grid in ONE call). Each map fills the SAME template.
    Guards the common mistake of cramming N cells into ONE map with fake keys (R, R_1, R_2 ...):
    that only fills the first R and silently drops the rest -> DUPLICATE products. Detect it and
    tell the model to pass a LIST instead."""
    import json as _json
    try:
        parsed = _json.loads(r_json) if isinstance(r_json, str) else r_json
    except Exception:
        parsed = None
    # batch: a list of R-maps -> a list of products
    if isinstance(parsed, list):
        out = [_sh(["fill", template, m if isinstance(m, str) else _json.dumps(m)]) for m in parsed]
        return {"result": out}
    # anti-pattern guard: one dict whose keys share a base label (R5, R5_1, R5_2 ...) = a grid crammed
    # into one map. Filling would use only the first key and drop the rest -> silent duplicate products.
    if isinstance(parsed, dict) and len(parsed) > 1:
        import re as _re
        bases = {_re.sub(r"_\d+$", "", k) for k in parsed}
        if len(bases) < len(parsed):
            return {"result": "ERROR: r_json crams several cells into one map (keys like R,R_1,R_2 share a "
                    "base label). fill would use only the first and drop the rest, producing DUPLICATE "
                    "products. Pass a JSON LIST of per-cell maps instead, e.g. "
                    '[{"R5":"*OC"},{"R5":"*C"},{"R5":"*Cl"}] -> one product per cell.'}
    return {"result": _sh(["fill", template, r_json if isinstance(r_json, str) else _json.dumps(r_json)])}


def t_check_full(out=None, **_):
    """DETERMINISTIC self-check on the emitted V2 (read from `out`, injected by the harness). Reports
    STRUCTURAL problems for YOU to fix -- it does NOT judge chemistry, only facts computable from the
    SMILES + R-maps. Issue types:
      - unfilled_layer: two examples share a template and have DIFFERENT markush_enumeration but the
        SAME product structure -> an R layer was recorded (markush_enumeration) but never FILLED into
        the structure. Materialize it: fill(template, that cell's FULL multi-key R-map).
      - leftover_markush: a concrete example product/reactant still contains '*'.
      - reactant_as_product: an example product is an onium SALT or equals a template REACTANT (a
        reactant got emitted as a product) -> replace with that cell's real drawn product.
      - duplicate: two examples with the SAME R-map AND same product (a real duplicate row) -> drop one.
      - missing_reactant: an example has no reactants.
    Call this AFTER emit_full; fix every issue, re-emit, and re-check until issues=[] before finish."""
    import json as _json
    try:
        v2 = _json.load(open(out))
    except Exception as e:
        return {"result": f"cannot read emitted V2 at {out}: {e}"}
    try:
        from rdkit import Chem
        from rdkit import RDLogger; RDLogger.DisableLog("rdApp.*")
    except Exception:
        return {"result": "rdkit unavailable"}

    def cn(s):
        s = (s or "").replace("<sep>", ".").replace("<dum>", "*").strip()
        if not s:
            return None
        m = Chem.MolFromSmiles(s)
        return Chem.MolToSmiles(m) if m else None

    ex = v2.get("selected_examples") or []
    tmpl_reactants = set()
    for t in (v2.get("reaction_templates") or []):
        for r in (t.get("reactants") or []):
            c = cn(r.get("E-SMILES") or r.get("smiles"))
            if c and "*" not in c:
                tmpl_reactants.add(c)
    from collections import defaultdict
    issues, groups = [], defaultdict(list)
    for e in ex:
        p0 = (e.get("products") or [{}])[0]
        pc = cn(p0.get("E-SMILES") or p0.get("smiles")) if isinstance(p0, dict) else None
        # a cell's INTENDED identity = its R-map AND its description (either can distinguish it). Using the
        # description too makes this ungameable: the model cannot dodge an unfilled-layer flag by deleting
        # markush_enumeration, because the descriptions (R2=H vs R2=Me ...) still differ. If they were made
        # identical too, the annotator loses the distinction -- so honest descriptions are the safe choice.
        rmap = _json.dumps(e.get("markush_enumeration") or {}, sort_keys=True, ensure_ascii=False)
        desc = (e.get("description") or "").strip()
        # a cell's full structural identity = product(s) AND reactant(s). Comparing BOTH is what avoids the
        # false positive on a REACTANT-side scope: when an R varies a reactant that does not change the product
        # (e.g. an ester H/Me/Et that leaves on cyclisation -> one product, different reactants + yields), the
        # cells share a product but DIFFER in reactants -> NOT an unfilled layer, a correct extraction. Only
        # cells with the SAME product AND the SAME reactants but a different intended identity are truly missing
        # a fill. Group ALL cells together (not by inherit_template) so template-id splitting can't dodge it.
        rxc = ".".join(sorted(x for x in (cn(r.get("E-SMILES") or r.get("smiles"))
                                          for r in (e.get("reactants") or []) if isinstance(r, dict)) if x))
        groups["_all"].append((e.get("instance_id"), pc, rxc, rmap, desc))
    for _g, cells in groups.items():
        seen = {}
        for iid, pc, rxc, rmap, desc in cells:
            if pc is None:
                continue
            key = (pc, rxc)                     # same product AND same reactants = the same drawn reaction
            if key in seen:
                piid, prmap, pdesc = seen[key]
                if rmap != prmap or desc != pdesc:
                    issues.append(f"unfilled_layer: '{iid}' and '{piid}' have the SAME product AND SAME reactants "
                                  f"but are meant to DIFFER ('{desc[:50]}' vs '{pdesc[:50]}') -- a varying R was "
                                  f"recorded but not materialized in any structure; if it is a PRODUCT-side R "
                                  f"build a template with that R as '*' and fill '{iid}', if it is a REACTANT-side "
                                  f"R put the differing group on the reactant.")
                else:
                    issues.append(f"duplicate: '{iid}' duplicates '{piid}' (same product, same reactants, same R, same desc) -- drop one.")
            else:
                seen[key] = (iid, rmap, desc)
    for e in ex:
        iid = e.get("instance_id")
        if not e.get("reactants") and not e.get("failed"):
            issues.append(f"missing_reactant: '{iid}' has no reactants.")
        for p in (e.get("products") or []):
            if not isinstance(p, dict):
                continue
            body = (p.get("E-SMILES") or "").split("<sep>")[0]
            if "*" in body:
                issues.append(f"leftover_markush: '{iid}' product still has '*': {body[:40]}")
            pc = cn(p.get("E-SMILES"))
            if pc and ("[N+]" in pc or "[P+]" in pc or "[S+]" in pc) and "." in pc:
                issues.append(f"reactant_as_product: '{iid}' product is an onium SALT (a reactant): {pc[:40]}")
            elif pc and pc in tmpl_reactants:
                issues.append(f"reactant_as_product: '{iid}' product EQUALS a template reactant: {pc[:40]}")
    return {"result": {"clean": not issues, "n_examples": len(ex), "n_issues": len(issues), "issues": issues[:40]}}


def t_transform(rxn_smarts, reactants, **_):                                                        # FORWARD reaction SMARTS: apply a reaction -> product(s)
    rl = reactants if isinstance(reactants, (list, tuple)) else [reactants]                          # accept a list OR one dot-joined string
    return {"result": _sh(["transform", str(rxn_smarts), *[str(x) for x in rl]])}
def t_transform_batch(rxn_smarts, reactant_sets, **_):                                              # BATCH forward transform: one SMARTS, many cells
    s = reactant_sets if isinstance(reactant_sets, str) else json.dumps(reactant_sets)              # [[r1,r2],[r1b,r2],...] -> per-cell products
    return {"result": _sh(["transform-batch", str(rxn_smarts), s])}
def t_enumerate(template_reactants, template_products, concrete_products, **_):                     # BATCH scope-grid reactant derivation
    s = lambda x: x if isinstance(x, str) else json.dumps(x)
    return {"result": _sh(["enumerate", s(template_reactants), s(template_products), s(concrete_products)])}
def t_validate(smiles_list, **_):                                                                   # BATCH canon+validate a whole list
    s = smiles_list if isinstance(smiles_list, str) else json.dumps(smiles_list)
    return {"result": _sh(["validate", s])}
def t_reaction_check(plan, **_):                                                                    # GT-FREE template-conformance self-check
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except Exception:
            try:
                import ast; plan = ast.literal_eval(plan)
            except Exception:
                return {"result": "reaction_check: could not parse plan; pass it as a JSON object."}
    plan = _destringify(plan) if isinstance(plan, dict) else plan
    try:
        sd = str(REPO / "scripts")
        if sd not in sys.path:
            sys.path.insert(0, sd)
        import reaction_check as RC
        r = RC.reaction_check(plan if isinstance(plan, dict) else {})
    except Exception as e:
        return {"result": f"reaction_check error: {type(e).__name__}: {e}"}
    if not r.get("applicable"):
        return {"result": f"reaction_check: not applicable ({r.get('reason')})."}
    return {"result": {"conform_rate": r["conform_rate"], "n": r["n"], "n_ok": r["n_ok"],
                       "n_product_off": r["n_product_off"], "n_reactant_off": r["n_reactant_off"],
                       "flagged": r["flagged"]}}
def t_cond(text, **_):               return {"result": _sh(["cond", text])}
def t_abbrev(label, online=False, **_):
    return {"result": _sh(["abbrev", label, *(["--online"] if online else [])])}

def t_legend(paper_dir, figure_idx, **_):
    """Crop the figure's usual LEGEND zones (top band + right strip) and RETURN a crop image so you can READ the
    symbol/abbreviation key (e.g. a filled circle = 2-naphthyl, 'Ar = ...', a ligand 'L1' with its structure).
    Read it, then bind each glyph to a *-fragment (abbrev(symbol) resolves common ones) and fill it into every
    cell that uses it. (The harness also reads the key up front and gives it to you as 'FIGURE LEGEND / SYMBOL KEY'.)"""
    out_dir = str(Path(paper_dir) / f"legend_{figure_idx}")
    r = _sh(["legend", str(Path(paper_dir) / f"{figure_idx}.png"), "--out-dir", out_dir, "--scale", 2])
    crops = r.get("crops") if isinstance(r, dict) else None
    paths = [(c.get("out") if isinstance(c, dict) else c) for c in (crops or [])]
    img = next((p for p in paths if isinstance(p, str) and os.path.exists(p)), None)
    return {"result": r, "image": img}

def _enrich_plan_bbox(plan, paper_dir, figure_idx):
    """Resolve each plan molecule's bbox_index -> {bbox, molparser_md5, molparser_markush} from
    the detections file BY INDEX, so the emitted V2 `_source` carries image provenance natively
    (this drives the reviewer's bbox overlay). Falls back to an exact raw-SMILES match for a
    molecule the model forgot to index. Pure enrichment; never raises into the rollout."""
    try:
        f = Path(paper_dir) / f"{figure_idx}_molparser.json"
        dets = json.load(open(f)) if f.exists() else []
    except Exception:
        return plan
    def _canon(s):
        try:
            from rdkit import Chem
            m = Chem.MolFromSmiles((s or "").split("<sep>")[0])
            return Chem.MolToSmiles(m) if m else None
        except Exception:
            return None
    by_idx, by_smi, by_canon = {}, {}, {}
    for d in dets:
        if not isinstance(d, dict):
            continue
        if d.get("bbox_index") is not None:
            by_idx[d["bbox_index"]] = d
        if d.get("smi"):
            by_smi.setdefault(d["smi"], d)               # fallback: exact raw-smi
            c = _canon(d["smi"])                          # robust: canonical-smi (drives the condition match)
            if c:
                by_canon.setdefault(c, d)
    # markush SCAFFOLD detections = the DRAWN generic structures (e.g. '*c1ccc(...C(=O)O2)cc1').
    # An R-variation cell (a scope row like R=OMe) has no box of its OWN, but its filled product IS an
    # instance of one of these drawn scaffolds -> give it THAT scaffold's real box (never the top template).
    scaffolds = []
    try:
        from rdkit import Chem as _C
        for d in dets:
            if not isinstance(d, dict) or not d.get("markush") or d.get("bbox") is None:
                continue
            s = (d.get("caption") or d.get("smi") or "").split("<sep>")[0]
            if not s or "*" not in s:
                continue
            q = _C.MolFromSmarts(s.replace("*", "[*]")) or _C.MolFromSmiles(s)
            if q is not None and q.GetNumAtoms() >= 5:    # skip tiny scaffolds (e.g. '*C(*)=C') that match everything
                scaffolds.append((q, q.GetNumAtoms(), d))
    except Exception:
        scaffolds = []
    def _scaffold(s):
        """The DRAWN scaffold a filled product belongs to = the most-specific (largest) markush
        detection whose skeleton is a substructure of it. Returns that detection or None."""
        if not scaffolds:
            return None
        try:
            from rdkit import Chem as _C
            m = _C.MolFromSmiles((s or "").split("<sep>")[0])
            if m is None:
                return None
            best = None
            for q, natoms, d in scaffolds:
                if m.HasSubstructMatch(q) and (best is None or natoms > best[0]):
                    best = (natoms, d)
            return best[1] if best else None
        except Exception:
            return None
    def fill(m, d, scaffold=False):
        if d.get("bbox") is not None:    m.setdefault("bbox", d["bbox"])
        if d.get("bbox_index") is not None: m.setdefault("bbox_index", d["bbox_index"])
        if d.get("md5") is not None:     m.setdefault("molparser_md5", d["md5"])
        if d.get("markush") is not None: m.setdefault("molparser_markush", d["markush"])
        if d.get("caption") is not None: m.setdefault("molparser_caption", d["caption"])  # raw OCSR (keeps <sep>+stereo)
        if d.get("smi") is not None:     m.setdefault("molparser_smi", d["smi"])           # OCSR expanded/canonical read
        if scaffold:
            m.setdefault("_scaffold_box", True)          # box is the drawn parent scaffold, shared by this R-series
    def _match(s):
        """Link a SMILES to its OWN detection box: exact raw-smi first, then canonical (a model
        re-typed structure still finds its own drawn box)."""
        head = (s or "").split("<sep>")[0]
        return by_smi.get(head) or by_canon.get(_canon(s))
    def visit(mols):
        # mutate in place: a molecule the model emitted as a BARE STRING (no bbox_index) still has
        # a drawn detection box -- match it by SMILES and PROMOTE it to a dict so the box attaches.
        # Without this, string-molecule figures (e.g. a scope grid emitted as plain SMILES) carry
        # zero bbox and the reviewer can't overlay any box. NB: fill() setdefault preserves any real box.
        if not isinstance(mols, list):
            return
        for i, m in enumerate(mols):
            if isinstance(m, str):
                d = _match(m)                          # own drawn box (exact/canonical)
                if d is not None:
                    nm = {"smiles": m}; fill(nm, d); mols[i] = nm
                else:
                    d = _scaffold(m)                   # else the DRAWN scaffold this R-instance belongs to
                    if d is not None:
                        nm = {"smiles": m}; fill(nm, d, scaffold=True); mols[i] = nm
                continue
            if not isinstance(m, dict):
                continue
            bi = m.get("bbox_index")                  # ocsr_caption/ocsr_smi still get attached (copy-recovery needs smi)
            d = by_idx.get(bi) if bi is not None else None
            if d is None:                                 # fallback: raw-smi then canonical (own box)
                d = _match(m.get("smiles") or m.get("E-SMILES") or "")
            if d is not None:
                fill(m, d)
            else:                                         # last: the drawn scaffold it is an R-instance of
                d = _scaffold(m.get("smiles") or m.get("E-SMILES") or "")
                if d is not None:
                    fill(m, d, scaffold=True)
    def visit_conditions(conds):
        # DRAWN reagents/ligands (L1..Ln in an optimization table) carry a `smiles` but no bbox —
        # the model rarely supplies the bbox. Backfill it deterministically by canonical-SMILES match
        # to a detection, exactly like molecule provenance. -> _conditions() turns bbox into _source.bbox.
        if not conds:
            return
        steps = conds if (conds and isinstance(conds[0], list)) else [conds]
        for step in steps:
            for c in (step or []):
                if not isinstance(c, dict) or c.get("bbox") is not None:
                    continue
                s = c.get("smiles") or c.get("smi") or c.get("E-SMILES")
                if not s:
                    continue
                d = by_canon.get(_canon(s)) or by_smi.get(str(s).split("<sep>")[0])
                if d is not None:
                    if d.get("bbox") is not None:        c["bbox"] = d["bbox"]
                    if d.get("bbox_index") is not None:  c.setdefault("bbox_index", d["bbox_index"])
    for grp in (plan.get("selected_examples") or []) + (plan.get("reaction_templates") or []):
        if isinstance(grp, dict):
            visit(grp.get("reactants")); visit(grp.get("products"))
            visit_conditions(grp.get("conditions"))
    return plan

# ----------------------------------------------------------------------------- detection overlay
_FONT_PATHS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
def _font(size):
    from PIL import ImageFont
    for p in _FONT_PATHS:
        try:    return ImageFont.truetype(p, size)
        except Exception: continue
    try:    return ImageFont.load_default(size=size)     # Pillow >= 10
    except Exception: return ImageFont.load_default()

def _score_color(score):
    try:    s = float(score)
    except Exception: s = 0.0
    if s >= 0.9: return (54, 200, 113)      # green  — trust the OCSR SMILES as-is
    if s >= 0.5: return (230, 162, 60)       # amber  — worth a glance
    return (240, 97, 109)                     # red    — low; verify if it matters

def annotate_detections(fig_png, dets, out_png):
    """Render the figure with EVERY detection's bbox + its index number drawn on, color-coded by
    OCSR confidence. This 'labeled overview' is handed to the model (so it maps index<->structure<->
    printed-ID by eye, not by pixel arithmetic) and shown to the human in the product. The printed
    number IS the bbox_index the model must carry for provenance. Pure rendering; never raises."""
    try:
        from PIL import Image, ImageDraw
        im = Image.open(fig_png).convert("RGB")
        W, _ = im.size
        dr = ImageDraw.Draw(im)
        lw = max(2, W // 500)
        fs = max(15, W // 55)
        font = _font(fs)
        for d in dets or []:
            if not isinstance(d, dict):
                continue
            bb, i = d.get("bbox"), d.get("bbox_index")
            if not (isinstance(bb, (list, tuple)) and len(bb) == 4):
                continue
            x0, y0, x1, y1 = (int(v) for v in bb)
            col = _score_color(d.get("score"))
            dr.rectangle([x0, y0, x1, y1], outline=col, width=lw)
            label = str(i if i is not None else "?")
            try:
                l, t, r, b = dr.textbbox((0, 0), label, font=font); tw, th = r - l, b - t
            except Exception:
                tw, th = int(fs * len(label) * 0.6), fs
            pad = max(3, fs // 5)
            cx, cy = x0, max(0, y0 - th - 2 * pad)        # chip just above the box, clamped on-screen
            dr.rectangle([cx, cy, cx + tw + 2 * pad, cy + th + 2 * pad], fill=col)
            dr.text((cx + pad, cy + pad), label, fill=(0, 0, 0), font=font)
        im.save(out_png, "PNG")
        return out_png
    except Exception:
        return None


def render_detections_grid(dets, fig_png, out_png, sub=(210, 180)):
    """Per detection, three panes side by side: the REAL figure crop (DRAWN) | RDKit render of OCSR's
    `caption` reading | RDKit render of OCSR's `smi` reading. The model compares BOTH OCSR readings against
    the drawing and uses whichever matches (they usually agree; on stereo/markush they differ and the
    header flags 'caption != smi'). Markush caption renders with * attachment points; smi is the filled
    form. Adjacency makes 'did OCSR read this right, and which reading is correct?' a one-glance check."""
    try:
        import re
        from PIL import Image, ImageDraw
        from rdkit import Chem
        from rdkit.Chem import Draw
        items = [d for d in (dets or []) if isinstance(d, dict)]
        if not items:
            return None
        fig = Image.open(fig_png).convert("RGB"); FW, FH = fig.size
        sw, sh = sub
        hh, cap_h, gap = 28, 18, 6                          # header / sub-caption / inter-pane gap
        cell_w, cell_h = sw * 3 + gap * 2, hh + sh + cap_h  # 3 panes: drawn | caption | smi
        per_row = 1 if len(items) <= 3 else 2
        rows = (len(items) + per_row - 1) // per_row
        canvas = Image.new("RGB", (per_row * cell_w, rows * cell_h), (255, 255, 255))
        dr = ImageDraw.Draw(canvas)
        font, small = _font(16), _font(12)
        def fit(img, w, h):                                 # thumbnail centered on a white tile
            t = Image.new("RGB", (w, h), (255, 255, 255))
            im = img.copy(); im.thumbnail((w - 4, h - 4))
            t.paste(im, ((w - im.width) // 2, (h - im.height) // 2)); return t
        def render_smiles(s):
            if not s: return None
            m = Chem.MolFromSmiles(s)                       # RDKit renders * as attachment points (markush OK)
            if m is None: return None
            try:    return Draw.MolToImage(m, size=(sw, sh))
            except Exception: return None
        def canon(s):
            try:    m = Chem.MolFromSmiles(s); return Chem.MolToSmiles(m) if m else None
            except Exception: return None
        for k, d in enumerate(items):
            r, c = divmod(k, per_row)
            ox, oy = c * cell_w, r * cell_h
            idx = d.get("bbox_index"); idx = k if idx is None else idx
            col = _score_color(d.get("score"))
            try:    sc = f"{float(d.get('score')):.2f}"
            except Exception: sc = "?"
            cap_smi = re.sub(r"<[^>]*>", "", (d.get("caption") or "").split("<sep>")[0]).strip()  # raw OCSR (keep *)
            smi_smi = (d.get("smi") or "").strip()
            differ = bool(canon(cap_smi) and canon(smi_smi) and canon(cap_smi) != canon(smi_smi))
            dr.rectangle([ox, oy, ox + cell_w, oy + hh], fill=col)
            dr.text((ox + 6, oy + 5), f"#{idx}   OCSR {sc}" + ("   caption != smi" if differ else ""),
                    fill=(0, 0, 0), font=font)
            bb = d.get("bbox")                              # pane 0: the real drawn structure, cropped
            if isinstance(bb, (list, tuple)) and len(bb) == 4:
                x0, y0, x1, y1 = bb; pw, ph = (x1 - x0) * 0.06, (y1 - y0) * 0.06
                crop = fig.crop((max(0, int(x0 - pw)), max(0, int(y0 - ph)),
                                 min(FW, int(x1 + pw)), min(FH, int(y1 + ph))))
                canvas.paste(fit(crop, sw, sh), (ox, oy + hh))
            else:
                canvas.paste(Image.new("RGB", (sw, sh), (245, 245, 245)), (ox, oy + hh))
            for pane, s in enumerate((cap_smi, smi_smi), start=1):   # pane 1: caption render, pane 2: smi render
                px = ox + pane * (sw + gap)
                img = render_smiles(s)
                if img is not None:
                    canvas.paste(img, (px, oy + hh))
                else:
                    tag = "markush" if (("*" in s) or d.get("markush")) else ("invalid" if s else "—")
                    dr.rectangle([px, oy + hh, px + sw, oy + hh + sh], outline=(220, 220, 220), width=1)
                    dr.text((px + sw // 2 - 22, oy + hh + sh // 2 - 7), tag, fill=(150, 150, 150), font=small)
            dr.text((ox + 6, oy + hh + sh + 2), "drawn (figure)", fill=(90, 90, 90), font=small)
            dr.text((ox + (sw + gap) + 4, oy + hh + sh + 2), "OCSR caption", fill=(90, 90, 90), font=small)
            dr.text((ox + 2 * (sw + gap) + 4, oy + hh + sh + 2), "OCSR smi", fill=(90, 90, 90), font=small)
            dr.rectangle([ox, oy, ox + cell_w - 1, oy + cell_h - 1], outline=(180, 180, 180), width=1)
        canvas.save(out_png, "PNG")
        return out_png
    except Exception:
        return None


def _as_list(x):
    """Coerce a field that SHOULD be a list into one. The server SDK / model type-confusion delivers e.g.
    conditions=True or metrics=['82%']; funneling through this stops `.get()`/iteration crashes that spiral
    a whole emit (a non-list -> [], a lone dict -> [dict])."""
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return [x]
    return []


def _as_dict(x):
    """A value that SHOULD be a dict; anything else -> {} so `.get()` never throws."""
    return x if isinstance(x, dict) else {}


def _backfill_conditions(plan):
    """Conditions are EXTRACTED per reaction, not invented. The reaction_template owns the conditions
    drawn on the top-scheme arrow; an example that doesn't show its OWN conditions inherits the
    TEMPLATE's (so each example stays self-contained). We never fill the template from the examples
    (it must be read off the top scheme), and never merge across examples that carry genuinely
    DIFFERENT conditions — only empty examples inherit, only from the canonical source."""
    exs = plan.get("selected_examples") or []
    tmpls = plan.get("reaction_templates") or []
    def nonempty(c):
        c = _as_list(c)                                        # bool/str/None -> [] (never iterate a non-list)
        return bool(c) and any(s for s in c)
    # per-template conditions keyed so an example's inherit_template is HONORED: an example inheriting
    # T2 must get T2's conditions, not "the first non-empty template's". Key by template_id, the
    # positional T1/T2 label, and the bare 0-based index (the model's inherit_template conventions).
    tconds = {}
    for k, t in enumerate(tmpls):
        t = _as_dict(t)
        c = t.get("conditions")
        if nonempty(c):
            if t.get("template_id"):
                tconds[str(t["template_id"])] = c
            tconds[f"T{k + 1}"] = c
            tconds[str(k)] = c
    # global fallback = the SOLE template's conditions, or (no template at all) a single shared example set.
    uniq_t = []
    for t in tmpls:
        c = _as_dict(t).get("conditions")
        if nonempty(c) and c not in uniq_t:
            uniq_t.append(c)
    only = uniq_t[0] if len(uniq_t) == 1 else None
    if only is None and not tmpls:
        sets = []
        for e in exs:
            c = _as_dict(e).get("conditions")
            if nonempty(c) and c not in sets:
                sets.append(c)
        only = sets[0] if len(sets) == 1 else None
    for e in exs:
        if not isinstance(e, dict) or nonempty(e.get("conditions")):
            continue
        inh = e.get("inherit_template")
        c = None
        if inh is not None and not isinstance(inh, bool):
            c = tconds.get(str(inh)) or (tconds.get(str(inh).upper()) if isinstance(inh, str) else None)
        if c is None:
            c = only                                           # no/unresolved inherit -> unambiguous single source only
        if c:
            e["conditions"] = c
    return plan


def _backfill_constant_reactants(plan):
    """Answer-free: a CONSTANT co-reactant drawn once on the top scheme (no '*' — the same molecule in every
    cell) must appear in every example that inherits that template. The enumerate/manual fallback can leave an
    example carrying only the per-cell VARYING reactant (the user-seen bug: a 2-component grid whose examples
    each lost the constant co-reactant). Restore the constant from the template. Operates ONLY on the model's
    own plan: copies a constant reactant the model already put in its OWN template into its OWN examples —
    never invents a structure, never adds a varying ('*') reactant, never duplicates one already present."""
    exs = plan.get("selected_examples") or []
    tmpls = plan.get("reaction_templates") or []
    if not exs or not tmpls:
        return plan
    try:
        from rdkit import Chem, RDLogger
        RDLogger.DisableLog("rdApp.*")
    except Exception:
        return plan                                            # no rdkit -> can't dedup safely -> no-op
    def smi(m):
        m = _as_dict(m)
        return (m.get("E-SMILES") or m.get("smiles") or "").split("<sep>")[0].strip()
    def canon(s):
        try:
            mol = Chem.MolFromSmiles(s)
            return Chem.MolToSmiles(mol) if mol else ""
        except Exception:
            return ""
    def is_constant(s):
        return bool(s) and "*" not in s and Chem.MolFromSmiles(s) is not None
    single = len(tmpls) == 1
    for ti, t in enumerate(tmpls):
        t = _as_dict(t)
        constants = []                                         # (template molecule dict, canonical smiles)
        for r in _as_list(t.get("reactants")):
            s = smi(r)
            if is_constant(s):
                c = canon(s)
                if c:
                    constants.append((r, c))
        if not constants:
            continue
        for e in exs:
            if not isinstance(e, dict):
                continue
            if not single:                                     # multi-template: only act on a resolvable id match
                inh = e.get("inherit_template")
                if str(inh) != str(ti) and inh not in (t.get("template_id"), t.get("id"), t.get("label")):
                    continue
            have = {canon(smi(r)) for r in _as_list(e.get("reactants"))}
            have.discard("")
            rl = e.get("reactants")
            if not isinstance(rl, list):
                rl = []
                e["reactants"] = rl
            for mol, c in constants:
                if c not in have:
                    rl.append(dict(_as_dict(mol)))              # copy keeps the template reactant's identifier/bbox_index
                    have.add(c)
    return plan


def _normalize_yields(plan):
    """Two answer-free fixes on the model's OWN metrics (never invents a yield that is absent):
    (1) a bare numeric yield gets its '%' back ('52' -> '52%') so it matches reported format;
    (2) a product's yield is propagated to another example that lists the SAME product identifier
    but carries no yield metric (over-decomposition can split one product across fragments)."""
    import re
    exs = plan.get("selected_examples") or []
    def is_yield(m): m = _as_dict(m); return (m.get("role") or m.get("type")) == "yield"   # non-dict metric -> False
    def ytext(m):    m = _as_dict(m); return (m.get("text") or m.get("content") or "").strip()
    def setytext(m, t):
        if "text" in m or "content" not in m: m["text"] = t
        else: m["content"] = t
    for e in exs:                                              # (1) % normalization
        for m in _as_list(_as_dict(e).get("metrics")):
            if is_yield(m):
                t = ytext(m)
                if t and re.fullmatch(r"\d+(\.\d+)?", t): setytext(m, t + "%")
    known = {}                                                 # product_id -> a yield metric we already have
    for e in exs:
        for m in _as_list(_as_dict(e).get("metrics")):
            if is_yield(m) and ytext(m):
                pid = str(_as_dict(m).get("of") or _as_dict(m).get("for_product") or "")
                if pid: known.setdefault(pid, m)
    for e in exs:                                              # (2) propagate by product id
        if not isinstance(e, dict):
            continue
        have = {str(_as_dict(m).get("of") or _as_dict(m).get("for_product") or "") for m in _as_list(e.get("metrics")) if is_yield(m)}
        for p in _as_list(e.get("products")):
            pid = str(_as_dict(p).get("identifier") or "")
            if pid and pid in known and pid not in have:
                e.setdefault("metrics", []).append(dict(known[pid])); have.add(pid)
    return plan


def _completeness_warnings(plan):
    """Answer-FREE self-check appended to the emit VERDICT: counts EMPTY fields the model routinely
    leaves blank (per-product yield/ee/dr metric; per-molecule identifier) and reminds it to read those
    OFF THE FIGURE. Reveals nothing about the GT (no values, no targets) — it only nudges the model to
    follow our own completeness mandate, the #1 V2 gap (yields dropped on screens/grids; labels dropped)."""
    exs = plan.get("selected_examples") or []
    tpls = plan.get("reaction_templates") or []
    warns = []

    def has_metric(e):
        ms = e.get("metrics")
        if isinstance(ms, dict):
            return any(k in ms for k in ("yield", "ee", "dr", "er", "yield_text"))
        if isinstance(ms, list):
            return any(isinstance(m, dict) and (m.get("role") or m.get("type"))
                       in ("yield", "ee", "dr", "er", "conversion", "selectivity") for m in ms)
        return bool(e.get("yield_text"))

    prod_ex = [e for e in exs if isinstance(e, dict) and (e.get("products") and not e.get("failed"))]
    no_metric = [e for e in prod_ex if not has_metric(e)]
    if prod_ex and no_metric:
        warns.append(
            f"{len(no_metric)}/{len(prod_ex)} product-bearing examples carry NO yield/ee/dr metric. "
            "Scope grids and condition/optimization screens print a yield (and often ee/dr) beside EVERY "
            "product -- read each one off the figure (or read_paper_text for SI yields) and add it to that "
            "example's metrics. Do NOT invent a value: only record a metric that is actually printed.")

    mols = [c for e in (exs + tpls) for side in ("reactants", "products")
            for c in _as_list(_as_dict(e).get(side)) if isinstance(c, dict)]
    no_id = [c for c in mols if not (c.get("identifier") and str(c.get("identifier")).strip())]
    if mols and len(no_id) >= max(2, len(mols) // 2):
        warns.append(
            f"{len(no_id)}/{len(mols)} molecules have no identifier. Set each molecule's identifier to its "
            "printed compound label/number from the figure (the boxed index/overview shows where each sits).")

    # NOTE: a markush_enumeration nudge was tried here and REMOVED — it pressured the agent mid-extraction and
    # regressed hard scope grids (bench_48_2 F1 0.724 -> 0.037 risky / 0.286 even de-risked) for a field with NO
    # score weight. markush_enumeration is better populated by a DELIBERATE tool call (reverse/fill, now working
    # natively) or the planned scaffold_check tool — not an emit-time nudge competing with the core extraction.
    return warns


def _merge_kept_fields(plan, out):
    """ANTI-CLOBBER: a later emit that DROPS a per-product yield/identifier the model already captured in an
    EARLIER emit (when the figure was fresh in context) must not erase it. Graft those kept fields from the prior
    V2 at `out` onto the new plan, aligned by example ORDER (+ product identifier for yields, position for
    identifiers). Deterministic — never INVENTS a value, only restores one the model itself previously emitted.
    Fixes the long-context failure where a final emit silently drops the printed yields it read 20 turns ago."""
    if not os.path.exists(out):
        return plan
    try:
        prev_exs = json.load(open(out)).get("selected_examples") or []
    except Exception:
        return plan
    new_exs = plan.get("selected_examples") or []
    _MROLE = ("yield", "ee", "dr", "er")

    def yields_of(e):
        d = {}
        for m in (e.get("metrics") or []):
            if isinstance(m, dict) and (m.get("role") or m.get("type")) in _MROLE:
                d[str(m.get("of") or m.get("for_product") or "")] = m
        return d

    gy = gi = 0
    for i, e in enumerate(new_exs):
        if i >= len(prev_exs):
            break
        pe = prev_exs[i]
        for side in ("reactants", "products"):                   # restore dropped identifiers FIRST (by position)
            ncs, pcs = e.get(side) or [], pe.get(side) or []     # — so a dropped yield can then be keyed by id
            for j, c in enumerate(ncs):
                if not (c.get("identifier") and str(c.get("identifier")).strip()) and j < len(pcs):
                    kid = pcs[j].get("identifier")
                    if kid and str(kid).strip():
                        c["identifier"] = kid; gi += 1
        prev_y = yields_of(pe)
        ms = e.get("metrics")
        if ms is None:
            ms = e["metrics"] = []
        # Restore ONLY when the new emit dropped ALL of this example's yields (the actual collapse failure). If it
        # kept any yield, leave it untouched — topping up per-product is unsafe when the yield<->product `of`
        # linkage is inconsistent and would DOUBLE-add (reps caught a 24-yields-for-12-examples bug).
        if isinstance(ms, list) and prev_y and not yields_of(e):
            for m in prev_y.values():
                ms.append(dict(m)); gy += 1
    if gy or gi:
        plan.setdefault("_meta", {})["_anticlobber"] = f"restored {gy} yields + {gi} identifiers from prior emit"
    return plan


def _destringify(o):
    """Recursively turn any string that ENCODES a dict/list (Python repr OR JSON) back into a real object.

    Older vertexai/aiplatform SDK builds (the server runs 1.135.0) serialize NESTED tool-arg objects as
    repr strings, not just the outer one. So a plan can arrive as a real dict whose `selected_examples`,
    a molecule `{...}`, or a `[[{role,text}]]` conditions block is still a STRING -> `_backfill_conditions`
    / `_normalize_yields` then call `.get()` on a str and the whole emit dies with
    "'str' object has no attribute 'get'", spiraling the run to a no-emit failure. Walking every level
    and parsing the structured-looking strings makes emit robust to that version skew."""
    if isinstance(o, str):
        s = o.strip()
        if len(s) >= 2 and s[0] in "{[" and s[-1] in "}]":
            import ast
            for parse in (json.loads, ast.literal_eval):
                try:
                    return _destringify(parse(s))
                except Exception:
                    pass
        return o
    if isinstance(o, dict):
        return {k: _destringify(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_destringify(v) for v in o]
    return o


def _template_check(plan):
    """GT-FREE self-consistency: flag examples whose product/reactants do NOT conform to their template (通式),
    or whose product carries a substituent absent from every reactant (reactant<->product disagreement). Returns
    a list of short warning lines for the emit verdict. Never raises into emit (a missing/broken checker is a no-op)."""
    try:
        sd = str(REPO / "scripts")
        if sd not in sys.path:
            sys.path.insert(0, sd)
        import reaction_check as RC
        r = RC.reaction_check(plan)
    except Exception:
        return []
    if not r.get("applicable") or not r.get("flagged"):
        return []
    lines = []
    for f in r["flagged"][:12]:
        if f["status"] == "product_off_template":
            lines.append(f"cell {f['id']}: PRODUCT does not match the template scaffold — re-read this product off the figure.")
        elif f["status"] == "reactant_off_template":
            lines.append(f"cell {f['id']}: a REACTANT does not match the template scaffold ({', '.join(f.get('off_template_reactants') or [])[:60]}) — re-derive it.")
        elif f["status"] == "R_inconsistent":
            lines.append(f"cell {f['id']}: product has a group {f.get('inconsistent_fragments')} absent from every reactant — reactant/product disagree, fix one.")
    return lines


def t_emit_full(plan, out, paper_dir=None, figure_idx=None, **_):
    """Write the plan, validate+assemble into V2, return the VERDICT/critique text + out path."""
    # Some vertexai SDK builds hand a nested object back as a STRING instead of a parsed dict — and it
    # can be a Python repr (single quotes), not strict JSON. Parse BOTH so a stringified plan doesn't
    # crash every emit ("'str' object has no attribute 'get'") and spiral the run to a no-emit failure.
    if isinstance(plan, str):
        parsed = None
        try:
            parsed = json.loads(plan)                      # strict JSON (double quotes)
        except Exception:
            try:
                import ast
                parsed = ast.literal_eval(plan)            # Python repr (single quotes) -> dict, safely
            except Exception:
                return {"result": {"verdict_report": "REJECTED: 'plan' arrived as a string that parsed as "
                        "neither JSON nor a Python literal (likely truncated). Re-call emit_full with plan "
                        "as a JSON OBJECT: {\"selected_examples\":[...], \"reaction_templates\":[...]}."}}
        plan = parsed
    if not isinstance(plan, dict):
        return {"result": {"verdict_report": f"REJECTED: 'plan' must be a JSON object, got {type(plan).__name__}."}}
    plan = _destringify(plan)                              # NESTED stringified objects (SDK version skew) -> real dicts/lists
    plan = _backfill_conditions(plan)
    plan = _backfill_constant_reactants(plan)                  # restore a constant co-reactant dropped from examples
    plan = _normalize_yields(plan)
    if paper_dir is not None and figure_idx is not None:
        plan = _enrich_plan_bbox(plan, paper_dir, figure_idx)
    plan = _merge_kept_fields(plan, out)   # anti-clobber: keep yields/identifiers a prior emit already captured
    # Guard: every emit lands at out_path (so run()'s validity check is honest), but LAST emit wins — so a
    # degenerate final emit (empty, OR a catastrophic SHRINK vs a good prior draft) must NOT clobber it. HP
    # especially can spiral (e.g. 35 emits) and ship a 1-example plan over a 13-example one -> F1 collapse.
    n_new = len(plan.get("selected_examples") or []) + len(plan.get("reaction_templates") or [])
    had = 0
    if os.path.exists(out):
        try:
            prev = json.load(open(out))
            had = len(prev.get("selected_examples") or []) + len(prev.get("reaction_templates") or [])
        except Exception:
            had = 0
    if had >= 6 and n_new * 2 < had:
        return {"result": {"verdict_report": f"REJECTED: this plan has only {n_new} example(s)/template(s) but the "
                f"kept draft at {out} has {had} — that SHRINK would destroy a good extraction. Re-emit the FULL "
                f"list (~{had}); if one cell is wrong, fix THAT cell inside the full list, don't drop the rest.",
                "out_path": out}}
    if n_new == 0 and had > 0:
        return {"result": {"verdict_report": f"REJECTED: this plan has 0 examples/templates, but a previous emit "
                f"with {had} example(s) is kept at {out}. Re-emit the FULL example list — do not send an empty plan.",
                "out_path": out}}
    pp = out + ".plan.json"; json.dump(plan, open(pp, "w"), indent=1, ensure_ascii=False)
    p = subprocess.run([PY, str(SKILL/"tool.py"), "emit-full", pp, out], capture_output=True, text=True)
    verdict = (p.stdout or p.stderr)[-2000:]
    warns = _completeness_warnings(plan)
    if warns:
        verdict += "\n\n[COMPLETENESS CHECK - fix before finish]\n" + "\n".join("- " + w for w in warns)
    tc = _template_check(plan)
    if tc:
        verdict += "\n\n[TEMPLATE CHECK - these cells do not conform to the reaction template; fix before finish]\n" + "\n".join("- " + w for w in tc)
    return {"result": {"verdict_report": verdict, "out_path": out}}

_META_PROMPT = (
    "You are an expert chemist reading ONE reaction figure. Do NOT output any SMILES or structures. Read every "
    "PRINTED label, yield and condition off the figure and return, for EVERY numbered product / table row / "
    "scope-grid cell, one object:\n"
    '{"identifier":"<the printed compound/entry label, e.g. 3, 13, 3a, entry 2>", "yield":"<printed yield with %, '
    'or null>", "ee":"<or null>", "dr":"<or null>", "conditions":[[{"role":"catalyst|ligand|base|reagent|solvent|'
    'temperature|time|atmosphere|additive","text":"..."}]]}\n'
    "Read the numbers EXACTLY as printed (a table's yield column, or the value beside each structure). "
    'Output ONLY a JSON object {"examples":[ ... ]} — no prose, no markdown.')


def _metadata_pass(paper_dir, figure_idx):
    """One-shot gemini read of the figure's printed METADATA (per-example identifier + yield/ee/dr + conditions),
    NO structures. Baseline gemini is reliable here (yields ~100%, completeness 100%) where the long tool-loop
    drops them under context load. Returns {identifier: {"metrics":[...], "conditions":[...]}}. Never raises."""
    fig = Path(paper_dir) / f"{figure_idx}.png"
    if not fig.exists():
        return {}
    try:
        from vertexai.preview.generative_models import Part
        resp = _vision_model().generate_content(
            [_META_PROMPT, Part.from_data(fig.read_bytes(), "image/png")],
            generation_config={"temperature": 0.0, "max_output_tokens": 8192,
                               "thinking_config": {"thinking_budget": 0}})
        text = getattr(resp, "text", "") or ""
    except Exception:
        return {}
    import re as _re
    t = _re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=_re.MULTILINE).strip()
    data = {}
    for cand in (t, (t[t.find("{"): t.rfind("}") + 1] if "{" in t and "}" in t else "")):
        try:
            data = json.loads(cand); break
        except Exception:
            continue
    out = {}
    for e in ((data.get("examples") or []) if isinstance(data, dict) else []):
        idv = str(e.get("identifier") or "").strip()
        if not idv:
            continue
        mets = []
        for role in ("yield", "ee", "dr"):
            v = e.get(role)
            if v and str(v).strip().lower() not in ("", "null", "none", "n/a", "nd"):
                mets.append({"role": role, "text": str(v).strip(), "of": idv})
        out[idv] = {"metrics": mets, "conditions": e.get("conditions") or []}
    return out


def _merge_metadata(v2, meta):
    """Fill MISSING yields / empty conditions in the agent's V2 from the metadata pass, keyed by product identifier.
    FILL-ONLY: never overrides a value the agent already has; never touches structures/SMILES."""
    gy = gc = 0
    for e in (v2.get("selected_examples") or []):
        pid = next((str(p.get("identifier")).strip() for p in (e.get("products") or [])
                    if p.get("identifier") and str(p.get("identifier")).strip()), None)
        md = meta.get(pid) if pid else None
        if not md:
            continue
        ms = e.get("metrics")
        has_y = isinstance(ms, list) and any(isinstance(m, dict) and (m.get("role") or m.get("type"))
                                             in ("yield", "ee", "dr") for m in ms)
        if not has_y and md.get("metrics"):
            if not isinstance(ms, list):
                ms = e["metrics"] = []
            ms.extend(dict(m) for m in md["metrics"]); gy += len(md["metrics"])
        if not (e.get("conditions") or []) and md.get("conditions"):
            mc = md["conditions"]                       # normalize to the canonical [[{...}]] shape the
            if isinstance(mc, dict):                    # engine emits, so recovery can't inject a flat/dict/str variant
                mc = [[mc]]
            elif isinstance(mc, str):
                mc = [[{"role": "reagent", "text": mc}]]
            elif isinstance(mc, list) and not any(isinstance(x, list) for x in mc):
                mc = [mc]
            elif not isinstance(mc, list):
                mc = []
            if mc:
                e["conditions"] = mc; gc += 1
    if gy or gc:
        v2.setdefault("_meta", {})["_metadata_pass"] = f"filled {gy} yields + {gc} condition-sets from a 1-shot read"
    return v2, gy, gc


_LABEL_PROMPT = (
    "Each detected structure in this figure is boxed and tagged with an INDEX number (the colored chip on the box). "
    "For EACH index, read the PRINTED COMPOUND LABEL/NUMBER printed beside or beneath THAT structure in the figure "
    "(e.g. '4aab', '3a', '12'). Compound labels are GLOBAL and frequently NON-CONTIGUOUS (e.g. 4aab, 4aac, 4aad, "
    "4aag - skipping 4aae/4aaf) - read each EXACTLY as printed, NEVER assume they run in sequence and never "
    "auto-increment. If a box is a generic/template structure or has no printed compound label, use null. "
    'Output ONLY JSON: {"labels":{"<index>":"<printed label or null>", ...}}.')


def _label_pass(paper_dir, figure_idx, dets):
    """ONE structure-free vision read of the PRINTED COMPOUND LABEL at each detection box -> {bbox_index: label}.
    The long tool-loop systematically MIS-MAPS labels on dense scope grids (it auto-increments 4aab,4aac,4aae...
    when the figure's labels are non-contiguous 4aab,4aac,4aag...) even though it reads every STRUCTURE correctly.
    This focused temp-0 read of the labeled overview (whose boxes already carry their index) recovers the true
    index->label map so a downstream DETERMINISTIC pass can place the right structure under each printed label.
    Never raises -> {} on any failure (then the corrector is a no-op)."""
    try:
        boxes = [d for d in (dets or []) if isinstance(d, dict)
                 and isinstance(d.get("bbox"), (list, tuple)) and len(d.get("bbox")) == 4
                 and d.get("bbox_index") is not None]
        if len(boxes) < 4:
            return {}
        overlay = Path(paper_dir) / f"{figure_idx}_annotated.png"
        if not overlay.exists():
            annotate_detections(str(Path(paper_dir) / f"{figure_idx}.png"), dets, str(overlay))
        if not overlay.exists():
            return {}
        from vertexai.preview.generative_models import Part
        idxs = sorted(int(b["bbox_index"]) for b in boxes)
        resp = _vision_model().generate_content(
            [_LABEL_PROMPT + " Cover every index in " + str(idxs) + ".",
             Part.from_data(overlay.read_bytes(), "image/png")],
            generation_config={"temperature": 0.0, "max_output_tokens": 4096,
                               "thinking_config": {"thinking_budget": 0}})
        text = getattr(resp, "text", "") or ""
    except Exception:
        return {}
    import re as _re
    t = _re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=_re.MULTILINE).strip()
    data = {}
    for cand in (t, (t[t.find("{"): t.rfind("}") + 1] if "{" in t and "}" in t else "")):
        try:
            data = json.loads(cand); break
        except Exception:
            continue
    out = {}
    for k, v in ((data.get("labels") or {}) if isinstance(data, dict) else {}).items():
        try:
            ki = int(k)
        except Exception:
            continue
        if v is not None and str(v).strip().lower() not in ("", "null", "none", "n/a", "nd"):
            out[ki] = str(v).strip()
    return out


_LEGEND_PROMPT = (
    "This chemistry figure may contain a LEGEND / SYMBOL KEY that DEFINES shorthand used INSIDE the drawn "
    "structures — e.g. a filled circle = 2-naphthyl, a square = a specific aryl, 'Ar = 4-MeOC6H4', 'R = various', "
    "or a ligand label ('L1') shown next to its drawn structure. Read ONLY such DEFINITIONS (a glyph/letter shown "
    "EQUAL TO a drawn group or a name). For each, return the printed symbol, its meaning, and its SMILES — with a "
    "single '*' at the attachment point if it is a SUBSTITUENT fragment (2-naphthyl -> *c1ccc2ccccc2c1), or a full "
    "SMILES if it is a standalone reagent/ligand. Do NOT list product compounds, yields, or condition text, and do "
    "NOT invent a key that is not drawn. If there is no symbol key, return an empty list. "
    'Output ONLY JSON: {"legend":[{"symbol":"<as printed>","meaning":"<text>","smiles":"<SMILES or null>"}, ...]}.')


def _legend_pass(paper_dir, figure_idx, dets):
    """ONE focused vision read of the figure's LEGEND / symbol KEY -> {symbol: {meaning, smiles}}. Scope and
    abbreviation figures define glyphs (a filled circle = 2-naphthyl) or letters (Ar = ...) that appear INSIDE the
    drawn structures; OCSR cannot resolve them, so a cell drawn with the glyph reads as a placeholder/wrong group.
    Capturing these bindings up front (and handing them to the agent) lets it substitute the right substructure
    instead of defaulting (e.g. to a methyl). Gated on multi-structure figures so we don't pay on simple ones.
    Never raises -> {} on any failure (then it is a no-op)."""
    try:
        boxed = [d for d in (dets or []) if isinstance(d, dict) and d.get("bbox_index") is not None]
        if len(boxed) < 5:                                    # only pay the call on scope/abbreviation-style figures
            return {}
        fig = Path(paper_dir) / f"{figure_idx}.png"
        if not fig.exists():
            return {}
        from vertexai.preview.generative_models import Part
        resp = _vision_model().generate_content(
            [_LEGEND_PROMPT, Part.from_data(fig.read_bytes(), "image/png")],
            generation_config={"temperature": 0.0, "max_output_tokens": 2048,
                               "thinking_config": {"thinking_budget": 0}})
        text = getattr(resp, "text", "") or ""
    except Exception:
        return {}
    import re as _re
    t = _re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=_re.MULTILINE).strip()
    data = {}
    for cand in (t, (t[t.find("{"): t.rfind("}") + 1] if "{" in t and "}" in t else "")):
        try:
            data = json.loads(cand); break
        except Exception:
            continue
    out = {}
    for item in ((data.get("legend") or []) if isinstance(data, dict) else []):
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").strip()
        meaning = str(item.get("meaning") or "").strip()
        smi = item.get("smiles")
        smi = str(smi).strip() if smi not in (None, "", "null", "None", "n/a") else ""
        if smi:                                               # keep only an RDKit-parseable fragment ('*' is fine)
            try:
                from rdkit import Chem, RDLogger
                RDLogger.DisableLog("rdApp.*")
                if Chem.MolFromSmiles(smi) is None:
                    smi = ""
            except Exception:
                pass
        if sym and (meaning or smi):
            out[sym] = {"meaning": meaning, "smiles": smi}
    return out


def _dedup_template_reactants(v2):
    """Drop a redundant GENERIC template reactant left on an example beside its SUBSTITUTED form. On a scope grid
    where a reactant VARIES per cell, the agent sometimes keeps the template's generic reactant (e.g. the parent
    2-aminobenzyl alcohol Nc1ccccc1CO) AND adds the cell's substituted version (Cc1cccc(N)c1CO) -> a spurious
    extra reactant that dents reactant precision. Remove X from an example only when canon(X) is a TEMPLATE
    reactant AND another reactant Y in the SAME example is a proper SUPERSTRUCTURE of X (X is a substructure of
    the larger Y) — i.e. the substituted form is present, so the generic copy is the un-replaced leftover. Pure
    cleanup; returns n_removed and mutates v2 in place. Never raises (RDKit-guarded)."""
    try:
        from rdkit import Chem, RDLogger
        RDLogger.DisableLog("rdApp.*")
    except Exception:
        return 0
    def _c(s):
        m = Chem.MolFromSmiles((s or "").split("<sep>")[0])
        return Chem.MolToSmiles(m) if m else None
    tmpl = set()
    for t in (v2.get("reaction_templates") or []):
        for r in (t.get("reactants") or []):
            c = _c(r.get("E-SMILES") or r.get("smiles") or "")
            if c:
                tmpl.add(c)
    if not tmpl:
        return 0
    removed = 0
    for e in (v2.get("selected_examples") or []):
        rs = e.get("reactants") or []
        info = [(r, _c(r.get("E-SMILES") or r.get("smiles") or "")) for r in rs]
        info = [(r, c, (Chem.MolFromSmiles(c) if c else None)) for r, c in info]
        drop = set()
        for i, (ri, ci, mi) in enumerate(info):
            if mi is None or ci not in tmpl:
                continue
            for j, (rj, cj, mj) in enumerate(info):
                if i != j and mj is not None and mj.GetNumHeavyAtoms() > mi.GetNumHeavyAtoms() \
                        and mj.HasSubstructMatch(mi):
                    drop.add(i); break
        if drop:
            e["reactants"] = [r for k, r in enumerate(rs) if k not in drop]
            removed += len(drop)
    return removed


def _assign_products_from_labels(v2, dets, label_map):
    """DETERMINISTIC scope-grid permutation fix. The agent reads each cell's STRUCTURE correctly but mis-maps which
    printed compound number it is (it auto-increments). Yields/conditions, however, are read correctly BY LABEL
    (they live on the example, anchored to the printed label). So we keep each example's label + yield + conditions
    and only SWAP IN the correct product structure for that label: structure<->label are both anchored to the
    detection's bbox (label_map: bbox->printed label; detection: bbox->structure), so detection-with-printed-label-L
    IS the structure for the example whose product identifier is L. Only PRODUCTS are touched; reactants are left.
    Answer-free: uses only the model's own two reads (structures + printed labels), never GT. Returns (v2, n_fixed)."""
    if not label_map:
        return v2, 0
    try:
        from rdkit import Chem
        from rdkit import RDLogger; RDLogger.DisableLog("rdApp.*")
    except Exception:
        return v2, 0

    def _clean(s):
        return (s or "").split("<sep>")[0].strip()

    def _canon(s):
        s = _clean(s)
        if not s or "*" in s:
            return None
        m = Chem.MolFromSmiles(s)
        return Chem.MolToSmiles(m) if m else None

    # printed label -> the detection structure drawn at that box (prefer the higher-confidence box on a collision)
    label_struct = {}
    for d in dets or []:
        if not isinstance(d, dict):
            continue
        lab = label_map.get(d.get("bbox_index"))
        if not lab:
            continue
        smi = _clean(d.get("smi")) or _clean(d.get("caption"))
        if not smi or "*" in smi or _canon(smi) is None:
            continue
        try:
            sc = float(d.get("score") or 0)
        except Exception:
            sc = 0.0
        prev = label_struct.get(lab)
        # deterministic on ties: higher score wins; equal score -> lower bbox_index then canonical smi,
        # so detection-list ordering can never change which structure is chosen across runs.
        if prev is None or sc > prev[1] or (
                sc == prev[1] and (str(d.get("bbox_index")), smi) < (str(prev[2]), prev[0])):
            label_struct[lab] = (smi, sc, d.get("bbox_index"))

    n_fixed = 0
    for e in (v2.get("selected_examples") or []):
        prods = e.get("products") or []
        if not prods or e.get("failed"):
            continue
        pid = next((str(p.get("identifier")).strip() for p in prods
                    if isinstance(p, dict) and str(p.get("identifier") or "").strip()), "")
        tgt = label_struct.get(pid)
        if not tgt:
            continue                                   # no printed-label box for this example's label -> leave it
        new_smi, _, bi = tgt
        p0 = prods[0]
        if not isinstance(p0, dict):
            continue
        cur = _canon(p0.get("E-SMILES") or p0.get("smiles"))
        if cur is not None and cur == _canon(new_smi):
            continue                                   # already the right structure -> no-op
        if "E-SMILES" in p0 or "smiles" not in p0:
            p0["E-SMILES"] = new_smi
        else:
            p0["smiles"] = new_smi
        # keep provenance consistent: the structure now comes from detection `bi`, so update _source
        # (box + OCSR read + origin) instead of leaving it pointing at the old detection, and never
        # add a contradictory top-level bbox_index (provenance lives in _source).
        if bi is not None:
            src = p0.setdefault("_source", {})
            src["bbox_index"] = bi
            det = next((d for d in (dets or []) if isinstance(d, dict) and d.get("bbox_index") == bi), None)
            if det:
                if det.get("bbox") is not None:      src["bbox"] = det["bbox"]
                if det.get("smi") is not None:       src["ocsr_smi"] = det["smi"]
                if det.get("caption") is not None:   src["ocsr_caption"] = det["caption"]
                if det.get("md5") is not None:       src["molparser_md5"] = det["md5"]
            src["origin"] = "molparser"
            src.pop("_bbox_inherited", None)
            p0.pop("bbox_index", None)
        n_fixed += 1
    if n_fixed:
        v2.setdefault("_meta", {})["_label_fix"] = (
            f"placed {n_fixed} product structures under their printed label (scope-grid structure<->label fix)")
    return v2, n_fixed


def _relabel_instances_from_labelmap(v2, label_map):
    """Write each example's instance_id from the figure's PRINTED compound label (recovered by
    _label_pass: bbox_index -> printed number), replacing the engine's ex1/ex2 fallback. When one
    box is reused by several examples (a markush cell enumerated to multiple products), the shared
    label gets an a/b/c suffix -> 5a,5b,5c. The product identifier is synced so metrics.of / viewer
    links resolve. Answer-free: uses only the model's own label read, never GT. Returns (v2, n)."""
    if not label_map:
        return v2, 0
    lm = {str(k): (str(v).strip() if v is not None else None) for k, v in label_map.items()}

    def _bidx(mol):
        if not isinstance(mol, dict):
            return None
        bi = mol.get("bbox_index")
        if bi is None:
            bi = (mol.get("_source") or {}).get("bbox_index")
        return None if bi is None else str(bi)

    from collections import defaultdict
    groups = defaultdict(list)                      # printed label -> examples that resolve to it
    for e in (v2.get("selected_examples") or []):
        lbl = None
        for p in (e.get("products") or []):
            bi = _bidx(p)
            cand = lm.get(bi) if bi is not None else None
            if cand and cand not in ("?", "None"):
                lbl = cand
                break
        if lbl:
            groups[lbl].append(e)
    n = 0
    for lbl, exs in groups.items():
        lbl_has_suffix = bool(lbl) and lbl[-1].isalpha()
        multi = len(exs) > 1                        # only suffix a/b/c when a box is genuinely reused
        for k, e in enumerate(exs):
            if not multi:
                new = lbl
            elif lbl_has_suffix or k >= 26:         # already lettered, or past 'z' -> numeric suffix stays unique
                new = f"{lbl}_{k + 1}"
            else:
                new = f"{lbl}{chr(97 + k)}"
            if str(e.get("instance_id") or "") == new:
                continue
            for p in (e.get("products") or []):     # sync the first product's identifier + keep metric link
                if isinstance(p, dict):
                    old_pid = p.get("identifier")
                    p["identifier"] = new
                    for m in (e.get("metrics") or []):
                        if isinstance(m, dict) and str(m.get("of")) == str(old_pid):
                            m["of"] = new
                    break
            e["instance_id"] = new
            n += 1
    if n:
        v2.setdefault("_meta", {})["_instance_relabel"] = f"set {n} instance_id(s) from printed compound labels"
    return v2, n


def _yield_gap(v2):
    """(missing, total) product-bearing non-failed examples lacking a yield/ee/dr — the metadata-recovery trigger."""
    n = miss = 0
    for e in (v2.get("selected_examples") or []):
        if not e.get("products") or e.get("failed"):
            continue
        n += 1
        ms = e.get("metrics")
        if not (isinstance(ms, list) and any(isinstance(m, dict) and (m.get("role") or m.get("type"))
                in ("yield", "ee", "dr", "conversion", "selectivity") for m in ms)):
            miss += 1
    return miss, n


def _finish_block_reason(out_path):
    """If the V2 at out_path is NOT emit-valid (>=1 example; no leftover markush '*' in a concrete example
    SMILES; every example SMILES RDKit-parseable), return a SHORT reason naming the first offending SMILES
    so the model fixes it before finishing. Mirrors score_v2.gate_valid (the G=0.10 gate) -> blocks the
    catastrophic 'finished on an invalid emit' (e.g. counterion [BF4-], unfilled '*'). Answer-free: inspects
    only the model's OWN output. Returns None when valid."""
    try:
        v2 = json.load(open(out_path))
    except Exception:
        return "no valid V2 has been written yet — call emit_full first."
    exs = v2.get("selected_examples") or []
    if not exs:
        return "the V2 has 0 selected_examples — emit_full with the full example list first."
    try:
        from rdkit import Chem
        from rdkit import RDLogger; RDLogger.DisableLog("rdApp.*")
    except Exception:
        Chem = None
    for e in exs:
        for side in ("reactants", "products"):
            for c in e.get(side) or []:
                s = (c.get("E-SMILES") or c.get("smiles") or "").replace("<sep>", "").strip()
                if not s:
                    continue
                if "*" in s:
                    return (f"unfilled markush '*' in an example SMILES ({s[:55]}). Fill the R group "
                            "(reverse/fill) or move the generic structure into a reaction_template, then re-emit.")
                if Chem is not None and Chem.MolFromSmiles(s) is None:
                    return (f"an example SMILES RDKit cannot parse ({s[:55]}). Common cause: a counterion "
                            "written compactly — use explicit atoms, e.g. F[B-](F)(F)F not [BF4-], "
                            "[Cl-] not Cl4. Fix it and re-emit.")
    # NOTE: we deliberately do NOT gate finish on the check_full heuristics. Its 'unfilled_layer' signal
    # FALSE-POSITIVES whenever a scope varies a REACTANT-side R that legitimately does not change the product
    # (e.g. Synfacts-2012-8-836: R2 = the alkynoate ester H/Me/Et -> the ester leaves on lactonisation, so all
    # cells share ONE product but DIFFERENT reactants + yields -- a CORRECT extraction). Blocking on that
    # forced the model to spiral/game a non-problem. check_full stays an ADVISORY tool the model may call;
    # the finish gate only enforces the objective emit-validity above (>=1 example, no '*', parseable SMILES).
    return None


def t_graph(v2_path, **_):           return {"result": _sh(["graph", v2_path])}

# ============================================================================ High-Precision tools
# Exposed to the model ONLY in mode='precision' (added to the tool table + declarations there).
# Each takes the per-run _sbx (the agent's host workspace); they refuse cleanly if it is missing.
_VISION = {}                                                 # cached vision model for semantic_check


def _vision_model():
    """gemini-3.1-pro-preview with thinking OFF (~4s/call AND accurate -- the recognition study's
    proven fast config). Separate from the main tool-loop model; reuses the harness creds (VERTEX_CREDS)."""
    if "m" not in _VISION:
        import vertexai
        from google.oauth2 import service_account
        from vertexai.preview.generative_models import GenerativeModel
        creds = service_account.Credentials.from_service_account_file(str(CREDS))
        vertexai.init(project=PROJECT, location=LOCATION, credentials=creds)
        _VISION["m"] = GenerativeModel("gemini-3.1-pro-preview")
    return _VISION["m"]


def t_verify_silyl(label, smiles, **_):
    """Cross-check an OCSR silyl-ether expansion against the drawn label (TES vs TBS etc.) -- the #1 motif."""
    return {"result": _sh(["verify-silyl", label, smiles])}


def t_render(smiles, name="mol", _sbx=None, **_):
    """Render a SMILES to a PNG in the workspace. Returns the PATH (not the image) -- use look() to see it,
    or let semantic_check compare it to the drawing for you."""
    if _sbx is None:
        return {"result": "render is only available in High-Precision mode"}
    try:
        from tools import render_smi
        out = _sbx.abspath(f"render_{name}.png")
        r = render_smi(smiles, out_path=out)
        return {"result": {"path": out, "smi_canonical": r.get("smi_canonical")}}
    except Exception as e:
        return {"result": f"render error: {e}"}


def t_look(path, _sbx=None, **_):
    """Load a workspace image into the conversation so you can SEE it. The ONLY image-injector in HP mode --
    keeps context lean (you spend image tokens deliberately, not on every crop/render)."""
    if _sbx is None:
        return {"result": "look is only available in High-Precision mode"}
    try:
        p = _sbx.abspath(path)
    except Exception as e:
        return {"result": f"path error: {e}"}
    if not os.path.exists(p):
        return {"result": f"no such file: {path}"}
    return {"result": {"shown": path}, "image": p}


def t_read_skill(path, _sbx=None, **_):
    """Read one methodology/example file from the skill (WORKFLOW.md / PITFALLS.md / CASES.md / examples/...).
    Progressive disclosure: pull the section the ROUTER points you to, when you hit that situation."""
    if _sbx is None:
        return {"result": "read_skill is only available in High-Precision mode"}
    return {"result": _sbx.read_skill(path)}


def t_ls(path=".", _sbx=None, **_):
    if _sbx is None:
        return {"result": "ls is only available in High-Precision mode"}
    return {"result": _sbx.ls(path)}


def t_read_file(path, _sbx=None, **_):
    if _sbx is None:
        return {"result": "read_file is only available in High-Precision mode"}
    return {"result": _sbx.read_file(path)}


def t_write_note(path, text="", _sbx=None, **_):
    if _sbx is None:
        return {"result": "write_note is only available in High-Precision mode"}
    return {"result": _sbx.write_note(path, text)}


def t_semantic_check(smiles, bbox_index, paper_dir=None, figure_idx=None, _sbx=None, **_):
    """SEMANTIC verify (what canon/cip cannot do): render the candidate SMILES, crop the DRAWN structure at
    bbox_index, and ask a vision model whether they are the SAME molecule. Use before rewriting a detection.
    Available in BOTH modes — in Fast the temp renders go in the job's paper_dir (no sandbox). The vision
    compare is a SUB-call that returns only a TEXT verdict, so it never bloats the main agent context."""
    _wp = (lambda n: _sbx.abspath(n)) if _sbx else (lambda n: str(Path(paper_dir) / n))   # sandbox in HP, paper_dir in Fast
    try:
        dets = json.load(open(Path(paper_dir) / f"{figure_idx}_molparser.json"))
        det = next((d for d in dets if d.get("bbox_index") == bbox_index), None)
    except Exception:
        det = None
    if not det or not det.get("bbox"):
        return {"result": f"no detection bbox for bbox_index={bbox_index} (cannot semantic-check without the drawn region)"}
    try:
        from tools import render_smi, crop_bbox
        from vertexai.preview.generative_models import Part
        drawn = _wp(f"drawn_{bbox_index}.png")
        cand = _wp(f"cand_{bbox_index}.png")
        crop_bbox(str(Path(paper_dir) / f"{figure_idx}.png"), det["bbox"], out_path=drawn)
        render_smi(smiles, out_path=cand)
        parts = [Part.from_data(Path(drawn).read_bytes(), "image/png"),
                 Part.from_data(Path(cand).read_bytes(), "image/png")]
        # 3-way compare: also render OCSR's own reading, so the model can localise WHO is wrong.
        ocsr_smi = det.get("smi") or det.get("caption")
        ocsr_line = ""
        if ocsr_smi:
            try:
                ocsr = _wp(f"ocsr_{bbox_index}.png")
                render_smi(ocsr_smi, out_path=ocsr)
                parts.append(Part.from_data(Path(ocsr).read_bytes(), "image/png"))
                ocsr_line = " IMAGE 3 = an RDKit render of OCSR's reading of the SAME structure."
            except Exception:
                pass
        q = ("IMAGE 1 = the structure DRAWN in the paper figure (this is the TRUTH). "
             "IMAGE 2 = an RDKit render of MY candidate SMILES." + ocsr_line + " "
             "Does IMAGE 2 depict the SAME molecule as IMAGE 1? Compare the skeleton, EVERY substituent / "
             "functional group, and stereochemistry. First line: SAME or DIFFERENT (IMAGE 2 vs IMAGE 1). "
             "Then list each concrete difference (atom / group / stereo)" +
             (", and state whether IMAGE 3 (OCSR) matches IMAGE 1 better than IMAGE 2 does." if ocsr_line else "."))
        resp = _vision_model().generate_content(
            [q] + parts,
            generation_config={"temperature": 0.0, "max_output_tokens": 4096,
                               "thinking_config": {"thinking_budget": 0}})
        ans = (getattr(resp, "text", "") or "")
        head = ans.upper()[:40]
        verdict = "DIFFERENT" if "DIFFERENT" in head else ("SAME" if "SAME" in head else "UNCLEAR")
        out = {"verdict": verdict, "detail": ans[:700], "drawn": drawn, "render": cand}
        if verdict == "DIFFERENT":
            # Make DIFFERENT actionable IN CONTEXT — the #1 HP gap was the model verifying then NOT fixing.
            out["action"] = (
                f"YOUR SMILES IS WRONG vs the drawing at bbox {bbox_index}. Do NOT keep it; do NOT finish with it. "
                "FIX IT NOW: apply the listed differences; if it is a silyl / protecting group call "
                "verify_silyl(label, smiles), otherwise crop+look at this bbox to read the exact skeleton/substituent; "
                "then re-emit the corrected structure (fix THIS molecule inside the full plan, keep all the others). "
                "If IMAGE 3 (OCSR) matches the drawing better than yours, adopt OCSR's reading.")
        return {"result": out}
    except Exception as e:
        return {"result": f"semantic_check error: {e}"}


TOOLS = {
  "read_detections": (t_read_detections, "Return cached MolParser OCSR detections (bbox_index, bbox, smi, score) for the figure, or a note if none.", {"figure_idx":"integer"}),
  "read_paper_text": (t_read_paper_text, "Search the paper text for a query and return a window of surrounding text (tables, conditions, IUPAC names). Empty query returns the head.", {"query":"string"}),
  "crop":            (t_crop, "Crop+zoom a region of the figure by pixel bbox [x0,y0,x1,y1] and RETURN THE IMAGE so you can verify OCSR (wedges, regiochemistry, chain length, abbreviations). Keep output <2000px.", {"figure_idx":"integer","bbox":"array","scale":"integer"}),
  "canon":           (t_canon, "RDKit canonical SMILES (validity check; keeps stereo). Use to confirm any SMILES is valid and *-free.", {"smiles":"string"}),
  "cip":             (t_cip, "CIP R/S labels of every stereocentre in a SMILES. Use to compare the OCSR-drawn stereo against a named compound's descriptors.", {"smiles":"string"}),
  "opsin":           (t_opsin, "Convert an IUPAC name (e.g. '(1R,5R)-2,2-dimethoxybicyclo[3.1.0]hexan-3-one') to canonical SMILES. GROUND TRUTH for stereochemistry — use whenever the caption/text names a stereocentre.", {"name":"string"}),
  "retro":           (t_retro, "Decompose a product into reactant fragments via a named/raw retro SMARTS.", {"name":"string","product":"string"}),
  "roundtrip":       (t_roundtrip, "Consistency check: does retro(product) reproduce the claimed reactants? Returns ok/diff.", {"name":"string","product":"string","reactants":"array"}),
  "reverse":         (t_reverse, "MarkushMap R-GROUP EXTRACTION: read the R value off ONE concrete drawn cell given the generic template. template = the markush E-SMILES (with * and <a>idx:Rlabel</a> tags — read_detections gives these for markush=true detections); concrete = a specific drawn product's clean SMILES. Returns {Rlabel: fragment}. Use on a SCOPE GRID to learn each cell's R from what is drawn.", {"template":"string","concrete":"string"}),
  "fill":            (t_fill, "MarkushMap R-GROUP FILL: substitute R values into the generic template to MATERIALIZE the concrete product(s). template = the markush E-SMILES. r_json is EITHER one map {\"R\":\"*OC\"} -> one product, OR (BATCH, use this for a scope grid) a JSON LIST of per-cell maps [{\"R\":\"*OC\"},{\"R\":\"*C\"},{\"R\":\"*Cl\"}] -> a LIST of products, one per cell. To fill a whole grid, pass the LIST — do NOT cram cells into one map with fake keys (R,R_1,R_2 ...): that fills only the first and DUPLICATES the rest (the tool rejects it). Set each example's markush_enumeration={Rlabel:fragment}.", {"template":"string","r_json":"string"}),
  "validate":        (t_validate, "BATCH-validate a WHOLE list of SMILES in ONE call (never canon one-by-one). Pass smiles_list = a JSON ARRAY of every product/reactant SMILES. Returns per item {input, canonical, valid, has_star}. Read it once: keep canonical for valid items, and FIX only the items where valid=false. This replaces calling canon repeatedly.", {"smiles_list":"string"}),
  "reaction_check":  (t_reaction_check, "GT-FREE self-check: verify EVERY example conforms to its reaction_template (通式) in ONE batch call before you finish. Pass plan = your full plan object {selected_examples, reaction_templates}. Returns flagged cells: product_off_template (the product is the wrong scaffold -> re-read it), reactant_off_template (a reactant is the wrong scaffold -> re-derive it), R_inconsistent (the product carries a group missing from every reactant -> the two sides disagree). conform_rate=1.0 means all cells are template-consistent. Call this after building the plan; fix flagged cells, then emit.", {"plan":"object"}),
  "enumerate":       (t_enumerate, "BATCH the whole SCOPE GRID at once — the reliable way to back-derive every concrete REACTANT (85% of our errors are these derived molecules). Pass template_reactants + template_products (the generic forms with * / [n*]) + concrete_products (a JSON ARRAY of EVERY drawn product SMILES). Returns per-cell {drawn_product, R_map, filled_reactants, roundtrip_ok, status}. For status='trusted' cells (the R round-tripped back to the drawn product = self-verified) USE filled_reactants AS-IS; for status='failed' cells reverse mis-read the scaffold so derive those reactants yourself. Prefer this over reasoning each reactant one-by-one. All three args are JSON arrays of SMILES strings.", {"template_reactants":"string","template_products":"string","concrete_products":"string"}),
  "cond":            (t_cond, "Structure a condition string into V2 list-of-list (outer=sub-step, inner=role-tagged). Splits on ';'/'then'/'N.)'.", {"text":"string"}),
  "abbrev":          (t_abbrev, "Resolve an abbreviation/reagent OR a legend-defined symbol (TES, PMB, MsCl, 'Ar', '2-naphthyl', ...) to a *-fragment / SMILES. Call it on any glyph/abbreviation a structure uses so you can expand it. online=true uses OPSIN/PubChem.", {"label":"string","online":"boolean"}),
  "legend":          (t_legend, "Crop the figure's LEGEND zones (top band + right strip) and RETURN the crop image so you can READ a symbol/abbreviation KEY (e.g. filled circle = 2-naphthyl, 'Ar = ...', a ligand 'L1' with its structure). Use when a structure contains a glyph/abbreviation you must expand; then bind each symbol to its *-fragment (abbrev resolves common ones) and fill it into every cell that uses it.", {"figure_idx":"integer"}),
  "emit_full":       (t_emit_full, "Assemble the plan dict into the full V2 schema and VALIDATE it (canonicalises every SMILES, flags '*'/invalid, checks graph). Returns VERDICT: VALID|INVALID + critique. Iterate until VALID. Plan schema: {is_multistep, _meta:{task_id,paper_id,anchor_image,anchor_figure,anchor_page,reaction_class,molparser_detection_count,identifier_map,texts}, selected_examples:[{label,inherit_template,reactants:[{smiles,identifier,bbox_index,amount}],products:[...],conditions:[[{role,text}]],metrics:{yield,ee},failed,description}] (amount = a reactant's drawn stoichiometry e.g. '1.2 equiv'), reaction_templates:[...], reaction_graph:[{edge_id,step_order,from_mols,to_mols,via_template,step_label}]}. Set bbox_index on each molecule that comes from a detection (preserves image provenance/highlight).", {"plan":"object","out":"string"}),
  "graph":           (t_graph, "Derive multistep topology (starts/finals/convergent/order) from an emitted V2's reaction_graph.", {"v2_path":"string"}),
  "transform":       (t_transform, "FORWARD reaction SMARTS (the opposite of retro): APPLY a reaction to reactant(s) -> product(s). rxn_smarts = a reaction SMARTS you choose for THIS reaction's bond change (e.g. cyclopropanation '[C:1]=[C:2].[C:3]=O>>[C:1]1[C:2][C:3]1'); reactants = list IN TEMPLATE ORDER. Use it (a) to CONSTRUCT a product a scope panel does NOT draw — read the drawn reactant + the panel's standard partner, apply the reaction (NOT Markush fill, which only substitutes on a shared scaffold); (b) as a FORWARD CHECK — apply the reaction to your reactants and confirm it reproduces your product. Generate candidates with a LOOSE smarts, then render+look (semantic_check) and keep the chemically sensible one — never hard-code selectivity into the smarts.", {"rxn_smarts":"string","reactants":"array"}),
  "transform_batch": (t_transform_batch, "BATCH the FORWARD transform across a whole REACTANT-scope grid in ONE call (the forward twin of enumerate): same rxn_smarts, many cells. rxn_smarts = the one reaction SMARTS for this scheme's bond change; reactant_sets = a JSON ARRAY of cells, each cell a list of reactant SMILES IN TEMPLATE ORDER (the drawn varied reactant + the panel's standard partner), e.g. [[\"C=Cc1ccccc1\",\"O=CC1CCNCC1\"],[\"C=Cc1ccc(F)cc1\",\"O=CC1CCNCC1\"]]. Returns per-cell {reactants, products} (or {error} for a cell that fails, isolated). Use this to CONSTRUCT every undrawn product of a reactant-scope panel at once instead of calling transform cell-by-cell; then render+look the chemically sensible product per cell. Never hard-code selectivity into the smarts.", {"rxn_smarts":"string","reactant_sets":"array"}),
  "semantic_check":  (t_semantic_check, "RENDER-COMPARE self-check (catches detail errors canon/cip cannot — spurious methyl, off-by-one chain length, trisubstituted-arene REGIOCHEMISTRY): crops the DRAWN structure at bbox_index, renders YOUR candidate, renders OCSR's reading, a vision model compares all three -> SAME|DIFFERENT + each difference + which reads better. Use SELECTIVELY (cost): only on a cell your cheaper signals FLAG — read_detections score<0.6, a '*' you resolved, a validate/roundtrip mismatch, or an ambiguous regiochemistry; crop+zoom it first. On DIFFERENT, fix THAT molecule and re-emit (keep the others). Do NOT call it on every cell.", {"smiles":"string","bbox_index":"integer"}),
  "check_full":      (t_check_full, "DETERMINISTIC self-check on the emitted V2 (reads the file you just wrote; no args needed). Reports STRUCTURAL problems for YOU to fix, computed from SMILES + R-maps (it does NOT judge chemistry): unfilled_layer (two cells share a template + have DIFFERENT markush_enumeration but the SAME product -> an R layer was recorded but never FILLED; materialize it with fill(template, that cell's full multi-key R-map)), leftover_markush ('*' left in a concrete product), reactant_as_product (a salt/onium or a template reactant emitted as a product), duplicate (same R + same product), missing_reactant. ALWAYS call this after emit_full; fix every issue (fill unfilled layers, replace wrong products), re-emit, and re-check until issues=[] before finish.", {}),
  "finish":          (None, "Call when the V2 at `out` is VALID and complete (every drawn compound + arrow + table row covered) AND check_full returned issues=[]. Provide a one-line summary.", {"summary":"string"}),
}

# Extra tools exposed ONLY in High-Precision mode (merged into the table + declarations when mode='precision').
HP_TOOLS = {
  "read_skill":     (t_read_skill, "Read ONE skill file on demand: WORKFLOW.md, PITFALLS.md, TOOLS.md, SCHEMA.md, RDKIT_RECIPES.md, CASES.md, or examples/<name>/walkthrough.md. Pull the file the ROUTER points to when you hit that situation — do NOT read them all (keeps context lean).", {"path":"string"}),
  "semantic_check": (t_semantic_check, "SEMANTIC verify (what canon/cip CANNOT do): crops the DRAWN structure at bbox_index (the truth), renders YOUR candidate SMILES, and renders OCSR's reading, then a vision model compares all three. Returns SAME|DIFFERENT (your candidate vs the drawing) + each per-atom/group/stereo difference + which reading matches better. Run this on EVERY non-trivial structure you emit, and fix+re-emit any DIFFERENT.", {"smiles":"string","bbox_index":"integer"}),
  "verify_silyl":   (t_verify_silyl, "Check an OCSR silyl-ether expansion against the drawn label (TBS/TES/TMS/TIPS) — catches the TESO-read-as-TBS class. label = the drawn abbreviation; smiles = the OCSR reading.", {"label":"string","smiles":"string"}),
  "render":         (t_render, "Render a SMILES to a PNG in your workspace; returns the PATH (not the image). Use look() to view it, or let semantic_check compare it to the figure for you.", {"smiles":"string","name":"string"}),
  "look":           (t_look, "Load a workspace image (a crop/render, by path) into the conversation so you can SEE it. The ONLY image viewer in High-Precision mode — spend image tokens deliberately.", {"path":"string"}),
  "ls":             (t_ls, "List your workspace directory (the crops/renders/notes you've saved).", {"path":"string"}),
  "read_file":      (t_read_file, "Read a text/JSON file you saved in your workspace.", {"path":"string"}),
  "write_note":     (t_write_note, "Persist a scratch note or intermediate plan to your workspace (offload it from context; reference it later by path).", {"path":"string","text":"string"}),
}

SYSTEM = """You extract EVERY reaction in ONE chemistry figure into our V2 JSON, using the tools.
You can SEE the figure. You are ALSO given a LABELED OVERVIEW: the same figure with every OCSR
detection's box + its index number drawn on it, colored by confidence (green = trust as-is,
amber/red = worth verifying). The printed number on each box IS its bbox_index. Use the overview to
map index <-> structure <-> printed ID/yield BY EYE — you do not need to crop to find what a box is.
When you call read_detections you also get an OCSR COMPARE GRID: for each detection index, three panes — the
figure crop (DRAWN) beside the RDKit render of OCSR's `caption` reading AND of OCSR's `smi` reading. Compare
BOTH renders to the drawing and use whichever matches (the two usually agree; the header flags 'caption != smi'
when they differ — common on stereo/markush). If neither matches, OCSR misread it — correct the SMILES. The
crop is provided, so this replaces most per-structure cip/crop verification — do NOT crop those again.
DO NOT GUESS SMILES — read them off OCSR detections / build them with tools.
The goal is a COMPLETE valid V2 (every product/arrow/row present), not a perfect one. Be fast.
THE OUTPUT IS THE FINAL CONCRETE REACTION EXAMPLES. A scope figure draws generic templates + a per-cell list
of R values; YOUR JOB is to MATERIALIZE each cell's concrete reactants AND products by filling EVERY R -- and a
single cell often varies on 2-3 positions at once (an aryl R, an inner R2=H/Me/Et, an R1), so it takes SEVERAL
fills, layer by layer, to reach the concrete structure. Reason from the figure how each example must be built,
then fill. A cell whose structure still equals the generic/base (R=H) form when its label says R=Me/Et is NOT
done -- the R MUST appear in the SMILES, not only in the description or markush_enumeration.

0. GLOBAL SURVEY & PLAN (do this FIRST, before extracting any SMILES). From read_detections + the labeled
   overview, survey the WHOLE figure and decide HOW MANY examples you must emit — one per product cell / table
   row / scheme step. Write a short PLAN as a checklist: one line per example = its printed label (or a position
   like 'row2 col3') + a one-line note of WHAT it is (e.g. 'aryl scope, 4-OMe') — NOT its SMILES yet. State the
   total count N. This N is your contract: the final V2 MUST contain that many examples. On a DENSE figure (many
   cells) this is what stops you from summarizing or losing track — you then fill the plan cell by cell, in
   reading order, taking each SMILES straight from its detection. Before you finish, RE-CHECK the plan: every
   listed item must have a corresponding example with a structure + label + (if printed) a yield; emit any that
   are missing. NEVER stop at 1-3 examples on a figure that clearly has many — an incomplete emit is a failure.
0b. FILL PLAN (do this right after the count plan, before emit — this is what makes multi-layer scopes come out
   concrete). For EACH scope family, reason from the figure and write, as a plan (still no full SMILES typing):
     (i) the ONE generic template (reactant + product) with EVERY position that varies across the family marked
         as its own '*' + <a>idx:Rlabel</a> tag — R1, R2, Ar, ... ALL of them, even a small inner sub-scope like
         'R2 = H/Me/Et'. Do NOT bake any varying axis into the scaffold.
     (ii) per cell, its FULL R-map = the value at EVERY one of those positions for that cell (a cell on 2 axes
         has a 2-key map, e.g. {"R1":"Ph","R2":"Me","Ar":"4-F-C6H4"}). Read these off the figure's row/column
         labels; where a position is constant across the family, its value is the same in every cell's map.
   Then EXECUTE the plan: fill(template, [full R-map per cell]) in ONE batch call per template (reactants and
   products), and use the FILLED SMILES as each cell's structures. This is the reliable path to concrete
   reactants+products — a few batched fills, planned up front, instead of improvising fills and dropping or
   duplicating the cells you could not fill. Only after every cell in the plan is FILLED to a concrete structure
   do you emit. (If a sub-scope is given only as a text table under one drawn base structure, it is still this:
   the drawn base is the scaffold, mark its varying atom '*', and fill each row's value.)

1. LOOK + classify the archetype:
   - SCOPE GRID = one generic scheme at the top + a grid of product structures each with an ID and a yield. THIS IS THE COMMON CASE -> use the SCOPE-GRID PLAYBOOK (step 3).
   - else: linear/convergent MULTISTEP | CONDITIONS-SCREEN table (one transformation, N rows = N examples) | SCALE-UP table.
2. DETECTIONS FIRST: call read_detections (it also returns the OCSR COMPARE GRID). Every detection with score >= 0.9 (green box) is a TRUSTWORTHY structure — USE ITS SMILES AS-IS. Each detection carries TWO readings: `caption` (raw OCSR E-SMILES — preserves stereo ring-closures + markush R-groups) and `smi` (canonical / markush-FILLED — can differ from what was drawn). The COMPARE GRID renders BOTH beside the drawn crop. USE WHICHEVER RENDER MATCHES THE DRAWING; they usually agree, and when the header flags 'caption != smi' look closely (caption is often the faithful one for stereo/markush, but let the DRAWING decide). Whatever you pick, emit a CLEAN SMILES (strip any <sep> / tags). Do NOT call canon/cip on each detection: emit_full ALREADY canonicalizes and validates EVERY SMILES in one shot, so per-structure verification is wasted turns/tokens. Reserve canon/cip/opsin for the rare specific structure you genuinely doubt. PREFER THE OVERVIEW OVER CROPPING: the boxed index numbers already tell you which structure is which and where each printed ID/yield sits — read them off the overview by eye. Crop is a LAST RESORT, only when you genuinely cannot read tiny text (an ID/yield digit) or must verify a flagged wedge/regiochem on an amber/red box. Never crop a green box. PROVENANCE (REQUIRED, do NOT rely on any post-processing): set EVERY product's "bbox_index" to the detection box of the structure DRAWN for it — the box is how the reviewer highlights the cell. When several rows share ONE drawn structure (a scope list where '5a/5b/5c' or 'R=OMe/Me/Cl' all sit under a SINGLE drawing), EVERY one of those examples carries THAT scaffold detection's bbox_index (e.g. all of 5a,5b,5c -> the box of structure 5) — so each cell's box lands on its real drawn structure, NEVER on the top generic template. A product with no bbox_index is an error unless it is genuinely not drawn anywhere. Reactants you build by retro/reasoning (not drawn as their own detection) carry no bbox_index.
2b. LEGEND / SYMBOL KEY (do this BEFORE you read structures): many figures define shorthand used INSIDE the drawn structures — a glyph (a filled circle, square, star), a letter group ('Ar', 'R'), or a ligand label ('L1') shown EQUAL to a drawn group. If a "FIGURE LEGEND / SYMBOL KEY" is given to you above (or you see such a key in the figure), then wherever a drawn product/reactant uses one of those symbols you MUST EXPAND it to its defined group BEFORE you emit. OCSR canNOT resolve these — a cell drawn with a glyph at a position will be read as a placeholder and DEFAULTED to the wrong group (commonly a methyl) if you do not substitute. Bind each symbol to its *-fragment (use the SMILES given in the key, or call abbrev(symbol); call legend() to re-read the key if you need to), then fill that fragment into EVERY cell that uses the symbol (it is usually a CONSTANT substituent shared across the whole scope — only the OTHER position varies per cell). NEVER emit a structure that still leaves a glyph/abbreviation unresolved or defaults its position to methyl/H.
3. SCOPE-GRID PLAYBOOK (most failures happen here — follow it exactly):
   a. The detections ARE your product list — use each detection's SMILES DIRECTLY as one PRODUCT. Do NOT canon them one-by-one; emit_full validates them all together.
   b. Build ONE generic reaction_template from the TOP scheme (read the header once). The template MUST carry the generic REACTANT and PRODUCT structures as E-SMILES (use a markush R / `*` for the position that varies per cell), read from the top scheme's DRAWN structures — set its bbox_index. A reaction_template with empty reactants/products (conditions only) is WRONG: it must have the generic structures, e.g. reactants:[{E-SMILES of reactant1}, {E-SMILES of reactant2}], products:[{generic product E-SMILES with R}]. KEEP THE VARYING POSITION GENERIC (`*`/R) in the TEMPLATE — do NOT collapse a variable substituent into a concrete small group: a reactant drawn as R-CH(OH)-CH3 (R varies) is `*C(O)C`, NEVER isopropanol `CC(C)O`. Reading a variable R as a plain methyl/H is a real, systematic error that then propagates to every cell.
   c. The two REACTANTS are GENERIC (drawn once at the top, not per cell). Every example reuses them with the per-product R: reactants = [reactant1_from_top, reactant2_from_top(with this product's R)], products = [this detection]. Use retro()/the top scheme to materialize the two concrete reactant SMILES; vary only the part that changes per product. REPLACE, DON'T APPEND: when a reactant varies per cell, put the cell's SUBSTITUTED reactant IN PLACE OF the generic template one — an example must NOT carry BOTH the generic template reactant and its substituted form (that spurious duplicate hurts precision). Each example has exactly the reactants that cell actually uses.
   c2. BACK-DERIVE THE REACTANTS IN ONE BATCH (this is where most errors are — the reactants are NOT drawn per cell, you must derive them): call enumerate(template_reactants, template_products, [EVERY drawn product SMILES]) ONCE. It returns, per cell, {filled_reactants, R_map, roundtrip_ok, status}. For status='trusted' cells the R is SELF-VERIFIED (filling the template back reproduced the drawn product) — USE filled_reactants AS-IS and move on; do NOT re-derive or re-check them cell-by-cell (that just burns tokens). For status='failed' cells reverse mis-read the scaffold — derive THOSE reactants yourself (reverse/fill that one cell, or reason from the top scheme). The FIGURE is the final authority: if a trusted reactant looks chemically wrong against the drawing, override it. Set markush_enumeration={Rlabel:fragment} from the R_map. (For a one-off cell you can still call reverse then fill directly.)
   d. Map each detection (by bbox position) to its printed ID and yield; yield -> metrics.
   e. 'ND'/'complex mixture'/'unsuccessful'/'no reaction' cells -> failed:true, products:[] (still an example; keep the reactants).
   f. EMIT A DRAFT EARLY: as soon as you have the product list + template, call emit_full with ALL examples, then fix only what the VERDICT flags. Do NOT perfect every cell before the first emit. The emit VERDICT runs a GT-free TEMPLATE CHECK — any cell it flags (product/reactant off the template scaffold, or a product group missing from the reactants) is a real OCSR/derivation error to fix; you can also call reaction_check(plan) yourself before finishing to batch-verify every cell conforms to the template.
   g0. PRODUCT-MARKUSH GRID (the SINGLE most error-prone case — handle it with fill, NEVER by re-typing): when the product is given as ONE generic markush product template (a `*`-bearing E-SMILES carrying TWO OR MORE distinct R positions, e.g. read_detections returns a markush=true product `*C1=C[C@H](*)[C@H](*)C1<sep><a>0:R[3]</a><a>4:R[1]</a><a>6:R[2]</a>`) and the individual product cells are NOT each drawn as their own detection (n product detections << n scope cells), DO NOT type each concrete product SMILES by hand and DO NOT canon them one-by-one — with 2+ R positions, hand-typing scrambles WHICH group lands at R1 vs R2 vs R3 and silently corrupts the regiochemistry of EVERY cell (the #1 product error on these grids). Instead, for EACH scope cell read its per-position R values off the figure/overview (the cell shows the varying groups beside its ID), build that cell's R_map = {R1:`*frag`, R2:`*frag`, R3:`*frag`}, and call fill(product_template, R_map) to MATERIALIZE that cell's product deterministically. The template's `<a>idx:Rlabel</a>` tags tell you which `*` is which Rlabel — keep the mapping straight. Set each example's markush_enumeration={Rlabel:fragment} and products=[the filled SMILES]. fill preserves the scaffold + stereo and only swaps the R's, so it cannot drift the way re-typing does. Reserve hand-typing for a product with no shared template at all.
   g. UNDRAWN PRODUCTS (the panel draws the varying REACTANT + a compound number, but NOT the product — OCSR finds 0 products there): CONSTRUCT the product, do not leave it blank. read the drawn reactant, then transform(rxn_smarts, [reactant, the panel's standard partner]) where rxn_smarts encodes THIS reaction's bond change (e.g. cyclopropanation '[C:1]=[C:2].[C:3]=O>>[C:1]1[C:2][C:3]1'). This is NOT Markush fill (no shared product scaffold to fill) — it is a real transformation. Pick the CORRECT standard partner for that sub-panel (read the panel header; a figure can mix panels). Generate with a loose smarts, then verify the candidate by eye (render/semantic_check) and keep the sensible one. Mark these products' provenance as constructed (not OCSR-drawn). For a WHOLE reactant-scope grid, do all cells in ONE call with transform_batch(rxn_smarts, [[reactant, partner], ...]) instead of transform cell-by-cell.
   h. FLAGGED-CELL RENDER-COMPARE (selective — the regiochemistry/detail catcher): for a cell your cheaper signals FLAG — read_detections score<0.9 (amber/red) with an ambiguous substituent position, a trisubstituted arene, a validate/roundtrip mismatch, or a '*' you just resolved — crop+zoom that one cell and call semantic_check(smiles, bbox_index); on DIFFERENT, fix THAT molecule per the action and re-emit (keep the others). Do NOT semantic_check green (score>=0.9) cells or every cell — it is a per-cell vision sub-call; spend it only where a signal points.
4. STEREO (critical): OCSR routinely draws the WRONG ENANTIOMER of cyclopropane/fused systems. First use the OCSR RENDER GRID — a flipped enantiomer shows as a MIRRORED wedge vs the figure at a glance. Then for EVERY compound whose IUPAC name (with R/S) appears in the caption or paper text, call opsin(name) and cip(ocsr_smiles); if the OCSR reading is the enantiomer of OPSIN, FLIP it (invert all @/@@). A consistent series shares one physical configuration. WHEN THERE IS NO IUPAC NAME (the common case on drawn-only stereo figures): you still MUST resolve each stereocentre from the DRAWING, not just accept the raw OCSR @/@@ — compare each stereocentre's wedge/hash in the figure against the OCSR render in the grid, and if they mirror, flip that centre. Only flip a centre when the render visibly disagrees with the drawing; leave genuinely different per-cell configurations alone.
5. CONDITIONS: list-of-list. Conditions that apply TOGETHER in one step go in ONE inner list, e.g. [[{role:catalyst,text:'Mo(CO)6'},{role:reagent,text:'KOH'},{role:solvent,text:'toluene'},{role:temperature,text:'150C'},{role:time,text:'12h'}]]. ONLY create a second inner list for a genuine SEPARATE sequential step (numbered '1) ... 2) ...' or 'then'). Do NOT put each reagent/role in its own sub-step. NEVER emit a condition as a bare number or index — conditions:[1,2,3,4,5,6] is WRONG and gets DROPPED; every condition MUST be a {role,text} object whose text is the ACTUAL reagent/catalyst/ligand/base/solvent/temperature/time/atmosphere read off the arrow (e.g. {role:base,text:'K2CO3 (2 equiv)'}). If the conditions are too small to read, use read_paper_text — do NOT substitute placeholder integers or leave them empty. The reaction_template CARRIES THE CONDITIONS drawn on the top-scheme arrow (catalyst/ligand/base/solvent/temperature/time) — it is the canonical source; NEVER leave the template's conditions empty. CAPTURE THE FULL ARROW, NOT JUST REAGENTS: the easy-to-miss conditions are the PERIPHERAL ones — ATMOSPHERE (role:atmosphere — 'O2', 'N2', 'Ar', 'air', often drawn as a balloon icon or 'O2 (balloon)'), TEMPERATURE (role:temperature — 'reflux', 'rt', '-78 C', a Δ/heat symbol), TIME (role:time), and LIGHT/wavelength ('hv', '440 nm', 'blue LED'). Read every label/icon on or beside the arrow and emit each as its own condition; dropping the atmosphere or 'reflux' is a frequent, real miss. Each example REPEATS those conditions (self-contained) UNLESS the figure shows that specific cell run under DIFFERENT conditions (its own method/temperature/reagent), in which case the example carries its OWN conditions. Put yield/ee/dr in metrics (one metric per product). ALWAYS record each product's yield WITH its '%' sign; if a yield is not printed beside the structure, look it up in the experimental section via read_paper_text. Every product that has a reported yield must carry a yield metric. IDENTIFIERS: every molecule with a printed compound label/number beside it in the figure (e.g. '3a', '12', 'SM') MUST carry that label as its identifier — for reactants AND products, in EVERY figure type (linear/multistep/conditions-screen/markush, not only scope grids); read it off the labeled overview by eye. DESCRIPTION: give EVERY example a short, non-empty description that captures WHAT DISTINGUISHES that cell — its R-group value(s) ('R1=OMe, R2=Cl'), its scope variable ('n=2', 'Ar=4-F-C6H4'), or its entry/variation for an optimization row. This is what an annotator indexes by, so a scope/markush cell must never be left blank; do NOT just repeat the compound number. REACTANT AMOUNT/STOICHIOMETRY: when a reactant (a coupling partner, the alkene/arene, an oxidant used as a reagent-reactant) is drawn with a loading label beside it — '1.2 equiv', '2.0 equiv', '3 mol', 'x equiv' — set that reactant's "amount" field to it (e.g. {smiles:..., identifier:..., amount:'1.2 equiv'}). The equivalents of the limiting/excess reactant are real experimental data; do NOT drop them and do NOT misfile them as a condition.
5b. CONDITIONS-SCREEN / OPTIMIZATION TABLE (a table of entries that all run the SAME transformation; columns like 'entry' / 'variation in conditions' / 'yield'): make ONE example PER ROW, every row INHERITING the SAME standard reactants + products + template — the row changes the CONDITIONS, not the structures. The point of such a table is WHICH ONE THING each row changed, so for EACH row you MUST capture that — never leave it blank and never just copy the standard conditions onto every row. Read the row's OWN text (its 'variation' cell + its entry/index label) off the figure / the labeled overview, or via read_paper_text when the print is tiny — do NOT infer it from yield. Then (i) write it VERBATIM into that example's description as the entry label + its variation (generic shape: 'entry <N>: <the row's stated change, e.g. a different ligand / base / solvent / catalyst loading / equivalents / temperature>', or 'entry <N>: control — <what was omitted>'); the standard/first row's variation is 'none (standard conditions)'. (ii) Reflect that ONE change in the example's conditions: swap ONLY the varied reagent/ligand/base/solvent/loading and KEEP every other standard condition. Carry each row's entry number and any ligand/compound labels (e.g. an L-series ligand ID) as identifiers. A row whose variation text is dropped is INCOMPLETE — recovering it is required, not optional polish. DRAWN REAGENTS/LIGANDS: when a label used in the conditions (a ligand 'L1'/'L2', a named catalyst, an additive) is DRAWN as a structure somewhere in the figure (its own detection bbox — these often head the table as a legend of L1..Ln), do NOT keep it as a bare text label: attach its structure to that condition entry as {role, text, smiles, bbox} — smiles = the detected SMILES for that label (read_detections), bbox = that detection's bbox. The condition schema supports this (a drawn reagent becomes a condition with E-SMILES + bbox); leaving a drawn ligand as text-only loses its structure (the verifier flags drawn-reagent-smiles=n). Bind each L-label to its drawn structure ONCE, then reuse that {smiles,bbox} on whichever row uses that ligand.
6. COMPLETENESS: ONE example per numbered product / arrow / table row — ALL of them (e.g. products 3..47 = 45 examples), each with its reactants, its product, AND its printed yield/ee/dr captured as a metric. A product that omits its figure-printed yield is INCOMPLETE, not merely imperfect: emit every cell's yield/dr/ee in the SAME first emit (one metric per product) and never drop yields to finish faster — on a scope grid the per-cell yields are required, not optional polish. Drawn impurities are co-products. Reagents/ligands/legend glyphs are NOT products — BUT a legend glyph that appears INSIDE a product/reactant structure (e.g. a filled circle standing for 2-naphthyl at a ring position) IS part of that molecule and must be EXPANDED to its defined group (see step 2b), not dropped.
6b. FAITHFULNESS — extract only what is DRAWN as a reaction structure; do NOT over-extract (this is the #1 precision killer on mechanistic/phys-org figures):
   - Reagents, catalysts, solvents and byproducts are CONDITIONS, never products/reactants — even when they are drawn or you can write their SMILES (e.g. orthoesters like HC(OEt)3/HC(OMe)3, amines, BF3.Et2O, eliminated EtOH/H2O). They go in conditions, not as molecules.
   - Do NOT invent species that are not drawn in the figure: no mechanistic intermediates, no Lewis-acid / charged adducts, no tautomers, no curved-arrow pushing products.
   - GRANULARITY: alternative ways to run the SAME transformation ('Method A' vs 'Method B', or different reagents that give the same product) are CONDITIONS of ONE reaction, NOT separate steps. Set is_multistep + reaction_graph edges ONLY for genuinely SEQUENTIAL reactions where one reaction's isolated product is the next reaction's substrate. When unsure, prefer FEWER steps (a single transformation is one example, graph_edges=0).
7. emit_full(plan, out): iterate until VERDICT: VALID (no '*', no invalid SMILES, >=1 example). For multistep set is_multistep + reaction_graph edges; then graph() to sanity-check topology. Each edge keeps from_mols/to_mols (identifiers) AND adds from_bbox/to_bbox = the bbox_index of each DRAWN from/to molecule (so the topology points to the exact drawn copy when one molecule is drawn more than once); omit the bbox for any molecule not drawn as its own detection.
7a2. FILL EVERY LAYER DOWN TO THE CONCRETE STRUCTURE (this is where multi-axis scopes are lost). A scope
   often varies on 2-3 positions at once (e.g. an aryl R5 AND an inner R2=H/Me/Et AND R1). The rule: build ONE
   generic product template that marks EVERY varying position as its own '*' with an <a>idx:Rlabel</a> tag
   (R1,R2,R5,...), and for EACH cell fill the cell's FULL multi-key R-map in one call, e.g.
   fill(template, [{"R1":"*c1ccccc1","R2":"*C","R5":"*OC"}, {"R1":"*c1ccccc1","R2":"*CC","R5":"*OC"}, ...]).
   Do NOT bake one axis into the scaffold and fill only the other -- that silently drops the baked axis so
   every cell in that sub-series collapses to the same structure. If a sub-scope is only given as a small table
   (e.g. "R2 = H, Me, Et" under one drawn base structure) it is STILL a fill: take the drawn base as the
   scaffold, mark the ONE atom where R2 attaches as '*<a>idx:R2</a>', and fill {"R2":"*C"} / {"R2":"*CC"} per
   cell. It is a few extra fill calls, not a re-read -- resolving an inner layer is the SAME fill you already
   used, applied to the inner position. Never leave a cell showing the base (R=H) structure when its label says
   R=Me/Et: that R MUST be materialized in the SMILES, not only recorded in the description/markush_enumeration.
7b. SELF-CHECK BEFORE FINISH (required): after emit_full is VALID, call check_full. It deterministically
   flags structural problems you cannot see by eye across many cells: unfilled_layer (two cells are meant to
   DIFFER by description/R but have the SAME product -> an inner R layer was not filled; fix it exactly as 7a2
   says: mark that position '*' and fill each cell's R), leftover_markush, reactant_as_product (a salt/reactant
   emitted as a product), duplicate, missing_reactant. Fix EVERY issue with real fills (do NOT dodge the flag by
   deleting a markush_enumeration or blanking a description -- the check reads the description too), re-emit, and
   call check_full again. LOOP until it returns issues=[]. Only then finish.
8. finish when VALID and COMPLETE and check_full returned issues=[].
BUDGET DISCIPLINE: the labeled overview + high-score detections usually remove the need to crop at all — aim for ZERO crops, at most ~2 and only to read tiny text or verify a flagged wedge. Do NOT verify structures one-by-one (no per-detection canon/cip loops) — go STRAIGHT from read_detections to ONE emit_full containing ALL examples, then fix only what the VERDICT flags. If you ever need to sanity-check SMILES validity before emitting, call validate ONCE on the whole list (it returns valid/canonical/has_star per item) instead of canon-ing each cell. On a scope grid that means emitting all N products in the first emit, not one at a time. The figure + overview (and OPSIN for names) is the authority over OCSR, prior outputs, and intuition."""

# High-Precision mode APPENDS this to the proven SYSTEM (it does not replace it). Adds the workspace,
# the new tools, a situation->action ROUTER, and the over-edit guardrail. Used as skill_text when
# mode='precision' (an explicit skill_text still overrides, e.g. for SkillOpt).
HP_APPENDIX = """

========================= HIGH-PRECISION MODE =========================
You also have a WORKSPACE (a filesystem you own) and extra verification tools. Use them to CONFIRM a
structure before you commit it — but stay SELECTIVE: most green (score>=0.9) detections are correct, and
deep-checking every one wastes turns. Verify what is uncertain or what you are tempted to change.

MANDATORY ORDER — completeness FIRST, verification SECOND (do NOT invert this):
  1. FIRST call emit_full with a COMPLETE draft: EVERY product / arrow / table row / compound present, exactly
     as Fast mode would, taking each SMILES STRAIGHT from read_detections. Do NOT semantic_check anything yet.
  2. ONLY after a complete valid V2 exists, semantic_check EVERY emitted structure that has a bbox_index —
     SKIP only the trivially-simple high-confidence ones (score>=0.95 AND no stereocentre AND not silyl/boron/
     metal/fused-polycyclic). On a hard figure (silyl, fused, low-score) that means MOST of the structures —
     do NOT stop after a handful: an unverified wrong structure is the whole failure mode HP exists to fix.
     For each DIFFERENT verdict, fix the SMILES per the listed differences and re-emit. Do NOT call finish
     until every non-trivial detection-backed structure has been semantic_checked and every DIFFERENT corrected.
     Token budget is SECONDARY to getting the structures right — spend the calls.
  NEVER let verification starve completeness. If you have emitted FEWER structures than there are detections /
  drawn products, STOP verifying and emit the rest NOW — a complete draft beats a deeply-verified fragment.
  Verification is a second pass over a COMPLETE draft, never a gate you apply one-structure-at-a-time while building.

TWO LAWS:
  L1. The DRAWING is the authority. Read structures off OCSR / the overview; the figure decides.
  L2. Do NOT rewrite the skeleton or molecular formula of a high-confidence (score>=0.9) detection UNLESS
      semantic_check returns DIFFERENT. Silently "correcting" a correct detection from memory is the #1 error.

ROUTER — when you hit one of these, read the skill file and run the tool, then act on the result:
  - score>=0.9 and simple .............. (nothing) -> use it, emit_full
  - score<0.9, OR you want to change a detection's formula/skeleton .. read_skill PITFALLS.md -> semantic_check(smiles, bbox_index)
  - silyl label drawn (TBS/TES/TMS/TIPS) .............. verify_silyl(label, smiles)
  - scope grid / a markush=true detection ............. read_skill WORKFLOW.md -> reverse(template, cell) then fill(template, R)
  - a named stereocentre in the caption/text .......... read_skill RDKIT_RECIPES.md -> opsin(name) + cip(smiles)
  - polycyclic / Bpin / metal / heavily fused ......... read_skill CASES.md -> crop -> look -> semantic_check

SEMANTIC CHECK (the verification canon/cip cannot do): semantic_check(smiles, bbox_index) renders your
candidate SMILES and compares it to the DRAWN structure at that detection with a vision model, returning
SAME | DIFFERENT plus each concrete difference. canon/cip only check validity/stereo syntax — a wrong-but-valid
SMILES passes them. If semantic_check says DIFFERENT, fix per the listed diffs and re-check.

WORKSPACE: crops and renders are saved to your workspace as PATHS (not auto-shown) to keep context lean.
  look(path) shows you one image. write_note(path, text) offloads a scratch plan; read_file/ls recall it.
  read_skill(path) pulls ONE methodology/example file on demand — read what the ROUTER points to, not all of them.

Workflow: emit a COMPLETE draft early (emit_full validates every SMILES at once), THEN semantic_check every
non-trivial detection-backed structure (see the MANDATORY ORDER) and re-emit fixes. Verify thoroughly — an
unverified wrong structure is the failure HP exists to catch — but when semantic_check says SAME, leave that
detection ALONE (do not "improve" a confirmed-correct structure into a wrong formula).
"""
SYSTEM_HP = SYSTEM + HP_APPENDIX


# ----------------------------------------------------------------------------- the loop
def _decls(mode="fast"):
    from vertexai.preview.generative_models import FunctionDeclaration
    table = {**TOOLS, **HP_TOOLS} if mode == "precision" else TOOLS
    out = []
    for name, (_fn, desc, params) in table.items():
        props = {}
        for k, t in params.items():
            props[k] = {"type": t} if t != "array" else {"type": "array", "items": {"type": "number"}}
        out.append(FunctionDeclaration(name=name, description=desc,
                   parameters={"type": "object", "properties": props}))
    return out

def _safe_text(resp):
    """resp.text raises if the candidate has no parts (e.g. MALFORMED_FUNCTION_CALL). Never raise."""
    try:    return resp.text or ""
    except Exception: return ""

def _finish_reason(resp):
    try:    return str(getattr(resp.candidates[0], "finish_reason", "") or "")
    except Exception: return ""


def _strip_check_full(system_text, tooltab):
    """Remove the MANDATORY check_full self-check loop (this is now the DEFAULT; set CHECK_FULL=1 to restore).
    The deterministic checker false-flags condition/optimization/ligand-screen tables (rows that legitimately
    share the SAME product + reactants and differ only in CONDITIONS) as 'unfilled_layer', which nags the model
    into abandoning a correct explicit emit for an inherit_template form the engine then fails to materialize
    (root-caused on 941df3c9: 0/38 products WITH check_full -> 38/38 WITHOUT). A/B on val (89 figs) showed
    aggregate f1_ns flat and composite r +0.006 with it removed, i.e. no cost + it kills a deterministic
    failure mode -- the 3rd NET-NEGATIVE proof (see also 177 label_fix, 836). This strips ONLY that pressure
    (STEP 7b + the finish/tool clauses + the tool itself); STEP 0b fill-plan, reaction_check, emit VERDICT are
    untouched. Trust the model's emit; keep only the objective emit-validity gate (leftover '*', unparseable
    SMILES)."""
    import re as _re
    s = system_text
    s = _re.sub(r"7b\. SELF-CHECK BEFORE FINISH.*?(?=\n8\. finish)", "", s, flags=_re.S)
    s = s.replace("8. finish when VALID and COMPLETE and check_full returned issues=[].",
                  "8. finish when VALID and COMPLETE.")
    tt = dict(tooltab)
    tt.pop("check_full", None)
    if "finish" in tt:
        fn = tt["finish"]
        tt["finish"] = (fn[0], fn[1].replace(" AND check_full returned issues=[]", ""), fn[2])
    return s, tt


def run(paper_dir, figure_idx, out_path, max_iters=30, max_tokens=400_000, model_name=None,
        _model=None, skill_text=None, on_step=None, should_stop=None, mode="fast",
        provider="gemini-vertex", api_key=None, base_url=None, azure_endpoint=None, api_version=None,
        use_detections=True, grid_big=False, understand_first=False, images_mode="full"):
    """skill_text (when given) replaces the baked SYSTEM as the system_instruction. This is what
    makes the harness a frozen rollout engine whose 'skill' is an EXTERNAL, trainable document —
    SkillOpt edits skill_text and re-rolls; the model + tools stay fixed."""
    paper_dir = str(Path(paper_dir).resolve())
    # High-Precision mode: give the agent a host workspace dir it owns (per job; no cross-job
    # collision -- it nests under this job's paper_dir). None in Fast mode -> tools that take
    # _sbx ignore it, so the Fast path is byte-for-byte unchanged.
    sbx = SandboxFS(Path(paper_dir) / "sandbox_ws", SKILL) if mode == "precision" else None
    tooltab = {**TOOLS, **HP_TOOLS} if mode == "precision" else TOOLS   # HP tools only in precision
    if not use_detections:
        # pure-vision ablation: even if a molparser.json exists on disk, the agent must NOT read OCSR — stub the
        # tool so read_detections returns nothing (the worker also skips writing the file on the product path).
        _pv = (lambda **_k: {"result": "OCSR is disabled for this run (pure model vision) — read every structure "
                             "directly from the figure and build it with the tools; no detections/bbox available."},
               TOOLS["read_detections"][1], TOOLS["read_detections"][2])
        tooltab = {**tooltab, "read_detections": _pv}
    # Provider abstraction (feature A): the agent's model call goes through a ModelBackend.
    #   provider='gemini-vertex' (default) = our managed Vertex path, byte-for-byte as before;
    #   provider='gemini-api'              = the user's OWN Gemini (BYO) key via google-genai (they pay).
    # The skill (SYSTEM) and the tool implementations stay provider-agnostic. _model (the offline _MockModel)
    # is injected straight into the Vertex backend. NOTE: the first message below has BARE-STRING parts (intro,
    # the LABELED OVERVIEW caption, the malformed-retry text); GeminiAPIBackend.send normalizes bare str -> a
    # text Part, so the same `msg` list works for both backends with no rewrap.
    from model_backend import make_backend, neutral_tool_decls
    _system = skill_text or (SYSTEM_HP if mode == "precision" else SYSTEM)
    # check_full removed by DEFAULT: the deterministic self-check is NET-NEGATIVE (it corrupts correct emits on
    # condition/optimization/ligand tables -> empty products, e.g. 941). Set CHECK_FULL=1 to restore the old loop.
    if os.environ.get("CHECK_FULL", "").lower() not in ("1", "true", "yes", "on"):
        _system, tooltab = _strip_check_full(_system, tooltab)
    if provider == "gemini-vertex":                             # managed Vertex: creds come straight from here
        backend = make_backend("gemini-vertex", system=_system, tool_decls=neutral_tool_decls(tooltab),
                               model_name=(model_name or MODEL), creds_path=str(CREDS), project=PROJECT,
                               location=LOCATION, model=_model)
    else:
        # registry model id (local-gemma/azure-chem/geek/byo-*) OR raw backend name. make_backend resolves
        # base_url / azure / extra_body / key-from-.env from model_registry; api_key + base_url/azure override for BYO.
        backend = make_backend(provider, system=_system, tool_decls=neutral_tool_decls(tooltab),
                               model_name=model_name, api_key=api_key, base_url=base_url,
                               azure_endpoint=azure_endpoint, api_version=api_version)

    fig_png = Path(paper_dir) / f"{figure_idx}.png"
    # Labeled detection overview (boxes + index numbers, colored by OCSR confidence). Rendered once;
    # reused if the front-door already wrote it. Attached so the model maps index<->structure by eye.
    overlay = Path(paper_dir) / f"{figure_idx}_annotated.png"
    # use_detections=False (EXPERIMENT: pure model vision, no OCSR interference): skip loading molparser
    # detections entirely -> dets=[] -> no labeled overview / compare grid / legend bind; the model reads
    # every structure from the figure itself and builds it with the tools. Default True = unchanged behavior.
    try:
        df = Path(paper_dir) / f"{figure_idx}_molparser.json"
        dets = (json.load(open(df)) if df.exists() else []) if use_detections else []
    except Exception:
        dets = []
    if dets and not overlay.exists():
        annotate_detections(fig_png, dets, str(overlay))
    intro = (f"Extract figure {figure_idx} of paper at {paper_dir} into V2; write the validated "
             f"V2 to {out_path}. The figure is attached. Paper text: "
             f"{next(iter(Path(paper_dir).glob('*_paper_text.txt')),'(none)')}.")
    if understand_first:
        # EXPERIMENT (understand-first): force a whole-figure comprehension pass BEFORE extracting — name the
        # reaction, explain what each region/structure means and how the scaffold relates to the scope cells,
        # then state the extraction plan. Tests whether deep figure understanding first improves accuracy.
        intro = ("STEP 0 — UNDERSTAND THE FIGURE FIRST (do this before any tool call): in 4-8 sentences, state "
                 "(a) what reaction/transformation this figure shows, (b) what each region means — the model/"
                 "scaffold reaction vs the substrate-scope grid vs any conditions/optimization table vs legend, "
                 "(c) what each recurring drawn structure or symbol represents and how the generic scaffold maps "
                 "onto each scope cell, and (d) your concrete plan for extracting every reaction. THEN proceed.\n\n"
                 + intro)
    msg = [intro, backend.image_part(fig_png.read_bytes())]
    if dets and overlay.exists() and images_mode != "lean":     # B (lean): drop the overlay (redundant w/ base+grid)
        msg.append("LABELED OVERVIEW (same figure; each detection's box + its index number, "
                   "colored by OCSR confidence: green = trust as-is, amber/red = verify). The "
                   "printed number on each box IS its bbox_index — carry it for provenance.")
        msg.append(backend.image_part(overlay.read_bytes()))
    if dets:                                                 # render the OCSR compare grid now; it is DELIVERED via
        # grid_big (EXPERIMENT): enlarge each compare pane from 210x180 to 360x300 to test the "crops are too
        # small to verify" hypothesis (dense 45-cell figures make the default panes tiny).
        _sub = (360, 300) if grid_big else (210, 180)
        render_detections_grid(dets, str(fig_png),          # read_detections (keeps the first message at 2 images, the
                               str(Path(paper_dir) / f"{figure_idx}_ocsr_grid.png"), sub=_sub)  # budget ran fine in v2/v3
    # DETERMINISTIC legend read: bind the figure's shorthand symbols (a filled circle = 2-naphthyl, 'Ar = ...')
    # up front and hand them to the agent as authoritative facts. OCSR cannot resolve a glyph drawn inside a
    # structure, so without this the agent defaults that position (the 49-2 class: every ●-cell became 2-methyl).
    legend_map = _legend_pass(paper_dir, figure_idx, dets) if dets else {}
    if legend_map:
        lines = []
        for sym, b in legend_map.items():
            lines.append("  " + sym + (" = " + b["meaning"] if b.get("meaning") else "")
                         + (" [" + b["smiles"] + "]" if b.get("smiles") else ""))
        msg.append("FIGURE LEGEND / SYMBOL KEY (authoritative) — these shorthand symbols/abbreviations appear "
                   "INSIDE the drawn structures. Wherever a product/reactant uses one, SUBSTITUTE the group below "
                   "before you emit; it is usually a CONSTANT substituent shared across the whole scope. Never "
                   "leave a symbol unresolved or default its position to a methyl/H:\n" + "\n".join(lines))

    spent_in = spent_out = spent_cached = 0                    # input / output / cached(prefix) tokens
    crop_n = emit_n = verify_n = 0                             # spiral guardrail counters
    nudge_n = 0                                                # how many times we've force-nudged toward emit
    emit_churn_n = 0                                           # nudges sent when the model RE-emits without finishing
    malformed_n = 0                                           # consecutive MALFORMED_FUNCTION_CALL retries
    empty_n = 0                                               # empty-STOP (no tool call + no text) retries, total per run
    finish_block_n = 0                                        # times we refused finish on an invalid emit (capped)
    _pruned = False                                           # C (progressive): images dropped from history after 1st draft
    transcript = []
    # FULL_TRANSCRIPT=1 -> keep each step's full input(args)+output(result)+reasoning(text) untruncated,
    # so the .transcript.json is a complete, human-debuggable trajectory (default stays compact for batch runs).
    _full_tr = bool(os.environ.get("FULL_TRANSCRIPT"))
    _ta, _tr, _tt = (200000, 200000, 200000) if _full_tr else (80, 300, 400)
    for it in range(max_iters):
        if should_stop and should_stop():                     # user pressed Stop -> end gracefully, keep partial draft
            step = {"iter": it, "stop": "user-cancelled"}
            transcript.append(step)
            if on_step:
                try: on_step(step)
                except Exception: pass
            break
        turn = None
        for _net_try in range(MODEL_RETRIES + 1):
            try:
                turn = backend.send(msg); break
            except Exception as e:                            # malformed/blocked response OR a transient net/quota drop
                es = str(e)
                # A transient tunnel/Vertex drop (HTTPSConnectionPool/429/503) must NOT kill the whole
                # extraction — under concurrency the shared egress drops some calls. Retry with backoff;
                # only a non-transient error (or exhausted retries) ends the run. (Product robustness: the
                # stress test showed ~1/3 of concurrent jobs dying on a single dropped first call.)
                if any(s in es for s in _MODEL_RETRY_SIGS) and _net_try < MODEL_RETRIES:
                    wn = {"iter": it, "warn": f"model call transient error, retry {_net_try + 1}/{MODEL_RETRIES}: {es[:120]}",
                          "tok_in": spent_in, "tok_out": spent_out}
                    transcript.append(wn)
                    if on_step:
                        try: on_step(wn)
                        except Exception: pass
                    time.sleep(2 * (_net_try + 1)); continue
                transcript.append({"iter": it, "error": es[:200]}); break
        if turn is None:                                      # all retries exhausted (or non-transient) -> end run
            break
        spent_in += turn.usage.get("in", 0) or 0
        spent_out += turn.usage.get("out", 0) or 0
        spent_cached += turn.usage.get("cached", 0) or 0      # implicit/explicit prefix cache hits
        spent = spent_in + spent_out
        calls = turn.tool_calls                               # neutral [{id,name,args}] from the backend
        if not calls:
            fr = turn.finish_reason
            # MALFORMED_FUNCTION_CALL: the model tried to call a tool but emitted broken/empty JSON
            # (common with very large emit_full args, or a thinking-model hiccup). Recoverable — nudge
            # it to re-issue ONE clean call (and shrink emit_full if that was it) instead of crashing.
            if "MALFORMED" in fr.upper() and malformed_n < 3:
                malformed_n += 1
                warn = {"iter": it, "warn": f"malformed function call — retry {malformed_n}/3",
                        "tok_in": spent_in, "tok_out": spent_out}
                transcript.append(warn)
                if on_step:
                    try: on_step(warn)
                    except Exception: pass
                msg = ["Your last function call was malformed or empty (MALFORMED_FUNCTION_CALL). "
                       "Re-issue exactly ONE well-formed function call now. If it was emit_full with a "
                       "large plan, split into fewer examples per call or shorten long fields."]
                continue
            # Empty STOP: candidate has no function call AND no text — a transient generation hiccup,
            # NOT a real finish (real completion is the finish TOOL). Re-prompt up to twice before
            # giving up (mirrors the MALFORMED retry); this is what dropped bench 10/3 to 0 examples.
            txt = turn.text
            # No tool call this turn. With NO draft yet this is a stall whether the turn was empty OR the
            # model narrated in PROSE (common on big/markush figs: it "plans" in text, blows the output-token
            # budget, and never calls emit_full -> 0 examples -> valid=False). FORCE the draft either way.
            if emit_n == 0 and empty_n < 4:
                empty_n += 1
                warn = {"iter": it, "warn": f"no tool call (finish={fr}, text={len(txt)}c) — force-emit {empty_n}/4",
                        "tok_in": spent_in, "tok_out": spent_out}
                transcript.append(warn)
                if on_step:
                    try: on_step(warn)
                    except Exception: pass
                # An empty turn this late almost always means the context is bloated (crops) and the model
                # is choking. Don't ask it to keep exploring — FORCE the draft. emit_full is one call and
                # ends the stall; it validates every SMILES itself, so a rough first draft is fine.
                msg = ["Your last turn produced NO tool call (empty or prose only) — you are stalling. STOP exploring/cropping/narrating. "
                       "Call emit_full RIGHT NOW with ONE example per detection (every product present), "
                       "taking each SMILES straight from read_detections. Do not verify first — emit_full "
                       "canonicalizes and validates everything, and you can fix VERDICT flags afterward."]
                continue
            transcript.append({"iter": it, "text": txt[:_tt] or f"(no content; finish={fr})"})
            break
        malformed_n = 0                                       # a clean tool turn resets the retry counter
        if spent > max_tokens:
            transcript.append({"iter": it, "stop": f"token cap {max_tokens} hit ({spent})"}); break
        # FULL_TRANSCRIPT: also record the model's reasoning PROSE that accompanies a tool-calling turn
        # (turn.text) -- otherwise the "why" is lost and only the structured call/args/result survive. This
        # is the narration the model emits alongside the call; keep it as its own step before the calls.
        if _full_tr:
            _rtxt = (getattr(turn, "text", "") or "").strip()
            if _rtxt:
                transcript.append({"iter": it, "reasoning": _rtxt[:_tt]})
        msg = []
        done = False
        for fc in calls:
            if not isinstance(fc, dict) or not fc.get("name"):        # a malformed tool call must not crash the loop
                transcript.append({"iter": it, "warn": f"malformed tool call skipped: {str(fc)[:80]}"})
                continue
            raw_args = fc.get("args")
            if isinstance(raw_args, str):                              # some backends stringify args -> parse back
                try: raw_args = json.loads(raw_args)
                except Exception: raw_args = {}
            if not isinstance(raw_args, dict):
                raw_args = {}
            name, args = fc["name"], dict(raw_args)
            args.pop("figure_idx", None); args.pop("paper_dir", None)  # fixed for this run; never let the model override (was a dispatch-collision bug)
            if name == "emit_full":   args["out"] = out_path  # force the validated V2 to land at out_path; the model sometimes invents a /tmp scratch path, which made run() report valid=False (os.path.exists(out_path)) for a genuinely-complete extraction (e.g. bench 10/5)
            if name == "check_full":  args["out"] = out_path  # always self-check the file we actually wrote
            if name == "crop":        crop_n += 1
            elif name == "emit_full": emit_n += 1
            if name in ("crop", "canon", "cip", "cond", "opsin", "read_paper_text"):
                verify_n += 1                                  # any re-read/verify; a pile of these with 0 emit = spiral
            step = {"iter": it, "call": name, "args": {k: str(v)[:_ta] for k, v in args.items()},
                    "tok_in": spent_in, "tok_out": spent_out, "tok_cached": spent_cached}  # live token totals
            transcript.append(step)
            if name == "finish":
                block = _finish_block_reason(out_path)          # don't let the model finish on an INVALID emit
                if block and finish_block_n < 3:
                    finish_block_n += 1
                    step["result"] = f"FINISH BLOCKED ({finish_block_n}/3): {block}"
                    if on_step:
                        try: on_step(step)
                        except Exception: pass
                    msg.append(backend.tool_result_part(name, {
                        "result": f"Cannot finish yet — {block} Re-emit the corrected plan, then finish."}))
                    continue
                done = True; step["result"] = "ok"
                if on_step:
                    try: on_step(step)
                    except Exception: pass
                msg.append(backend.tool_result_part(name, {"ok": True})); continue
            if name == "crop" and crop_n > 2 and emit_n == 0:
                # Each crop adds a big image; a pile of them bloats context and makes the model return
                # EMPTY responses (finish=STOP, no content) -> the run stalls with no emit. Cap crops:
                # refuse the 3rd+ and push to emit. Keeps context lean so generation doesn't choke.
                r = {"result": "CROP LIMIT (2) REACHED — more crops bloat the context and stall generation. "
                     "STOP cropping. Call emit_full NOW with one example per detection / grid cell, taking "
                     "each SMILES straight from read_detections (the green boxes are trustworthy as-is)."}
            else:
                entry = tooltab.get(name)                       # unknown tool name -> tell the model, don't KeyError-crash
                if entry is None:
                    r = {"result": f"unknown tool '{name}'. Valid tools: {', '.join(sorted(tooltab))[:200]}"}
                else:
                    try:
                        r = entry[0](paper_dir=paper_dir, figure_idx=figure_idx, _sbx=sbx, **args)
                    except Exception as e:
                        r = {"result": f"tool error: {e}"}
            if not isinstance(r, dict):                          # a tool returning None/""/[] must not crash r.get below
                r = {"result": "" if r is None else str(r)}
            step["result"] = str(r.get("result"))[:_tr]         # record WHAT each tool returned, so the optimizer can see the trajectory (not only the final score)
            if r.get("image"):
                step["image"] = True
            if on_step:                                        # live progress for the product UI (no behavior change to rollouts)
                try: on_step(step)
                except Exception: pass
            msg.append(backend.tool_result_part(name, {"result": r.get("result")}))
            if r.get("image") and os.path.exists(r["image"]):
                msg.append(backend.image_part(Path(r["image"]).read_bytes()))  # let it SEE the crop/grid
                if r.get("caption"):
                    msg.append(r["caption"])                # text guidance that goes WITH the image (e.g. compare-grid how-to)
        # C (progressive-focus EXPERIMENT): once the model has a first complete draft (emit_full done), the big
        # reference images (compare-grid + accumulated crops) have served their purpose. Drop them from the chat
        # history (keep only the base figure) so refinement happens on a LEAN, focused context — tests whether a
        # pile of overlapping figures dilutes attention / causes cross-cell confusion. The model can re-crop a
        # specific cell on demand (that crop re-enters, then gets pruned on the next draft). Vertex-only; no-op else.
        if images_mode == "progressive" and emit_n >= 1 and not _pruned:
            try: backend.prune_images(keep_first=1)
            except Exception: pass
            _pruned = True
        # SPIRAL guardrail: the model keeps VERIFYING (crop/canon/cip/cond/opsin/read_paper_text) but
        # never drafts -> burns to the token cap with NO output. This is the #1 failure on dense /
        # stereo-heavy figures. Count ALL verification re-reads (not just crop), and escalate: a firm
        # nudge at 6, a LAST-CHANCE nudge at the next threshold. Runtime nudge, not skill text.
        if not done and emit_n == 0 and verify_n >= 6 and nudge_n < 2:
            lead = (f"STOP verifying. You have made {verify_n} verification calls and emitted ZERO times."
                    if nudge_n == 0 else
                    f"LAST CHANCE: emit_full RIGHT NOW or this run fails with NO output ({verify_n} verify "
                    f"calls, 0 emits).")
            msg.append(lead + " Call emit_full NOW with a COMPLETE draft (one example per detection / grid "
                       "cell, every product present), taking each SMILES straight from read_detections. "
                       "emit_full canonicalizes and validates EVERY SMILES in ONE shot — per-structure "
                       "canon/cip/cond is wasted work. Fix only what the VERDICT flags afterward.")
            nudge_n += 1
        # EMIT-CHURN guardrail: the model keeps RE-EMITTING without finishing -> burns to the token cap (HP
        # spirals worst: 35 emits on bench_25_1). If a VALID V2 already exists, push it to FINISH and to stop
        # chasing SOFT completeness hints; re-emit only for a specific KNOWN-wrong structure. (FIX4 already
        # protects the draft from a shrink-clobber; this stops the wasted churn that gets there.)
        if not done and emit_n >= 15 and emit_churn_n < 2 and _finish_block_reason(out_path) is None:
            msg.append(f"You have called emit_full {emit_n} times and the V2 at out is already VALID. STOP "
                       "re-emitting the whole plan. If every drawn structure/row is present, call finish NOW. "
                       "Re-emit ONLY to fix a SPECIFIC structure you KNOW is wrong (e.g. a semantic_check "
                       "DIFFERENT) — do NOT keep re-emitting to chase soft completeness hints or minor wording.")
            emit_churn_n += 1
        if done:
            break
    # METADATA RECOVERY (deterministic, structure-free): if the lossy tool-loop dropped a chunk of the printed
    # yields, recover them with ONE structure-free read — gemini reads yields/conditions reliably in a single shot
    # where the long loop drops them — and fill them in BY IDENTIFIER (fill-only; never touches the agent's
    # structures). Gated on a real gap so we don't pay the extra call when the agent already captured them.
    if os.path.exists(out_path):
        try:
            v2 = json.load(open(out_path))
            miss, npx = _yield_gap(v2)
            if miss >= 2 and miss >= npx * 0.2:
                meta = _metadata_pass(paper_dir, figure_idx)
                if meta:
                    v2, gy, gc = _merge_metadata(v2, meta)
                    if gy or gc:
                        json.dump(v2, open(out_path, "w"), ensure_ascii=False, indent=1)
                    step = {"iter": it + 1, "call": "metadata_recovery",
                            "result": f"yield gap {miss}/{npx} -> filled {gy} yields + {gc} conditions (1-shot read)"}
                    transcript.append(step)
                    if on_step:
                        try: on_step(step)
                        except Exception: pass
        except Exception:
            pass
    # SCOPE-GRID LABEL FIX (deterministic): the long loop reads every cell's STRUCTURE right but mis-maps which
    # printed compound NUMBER it is (auto-incrementing past non-contiguous labels), while reading yields correctly
    # BY LABEL. One focused structure-free read recovers each box's printed label; we then place the right
    # structure under each label (keeping the example's label/yield/conditions). Gated on a labelled grid so we
    # don't pay the call on linear/single-reaction figures. Never overrides yields or reactants.
    if os.path.exists(out_path):
        try:
            v2 = json.load(open(out_path))
            labelled = [e for e in (v2.get("selected_examples") or [])
                        if (e.get("products") and not e.get("failed")
                            and any(isinstance(p, dict) and str(p.get("identifier") or "").strip()
                                    for p in e["products"]))]
            df = Path(paper_dir) / f"{figure_idx}_molparser.json"
            dets = json.load(open(df)) if df.exists() else []
            boxed = [d for d in dets if isinstance(d, dict) and d.get("bbox_index") is not None]
            if len(labelled) >= 4 and len(boxed) >= 4:
                label_map = _label_pass(paper_dir, figure_idx, dets)
                nfix = nrel = 0
                # OFF by default (set LABEL_FIX=1 to re-enable): this deterministic label->structure
                # OVERWRITE corrupts a CORRECT model emit whenever the model's auto-incremented cell
                # labels collide with the figure's real compound numbers -- e.g. a scope cell the model
                # labeled '3' gets its product overwritten by "compound 3" (which in Synfacts-2008-1-177
                # is the ammonium-salt REACTANT), and cells labeled '4'/'6' get overwritten by the
                # divergent products 4/6, producing salt-as-product + duplicates + dropped scope cells.
                # The model reads structures correctly; trust its emit. We still PERSIST the label map
                # below (metadata only) for image-anchored scoring.
                if os.environ.get("LABEL_FIX"):
                    v2, nfix = _assign_products_from_labels(v2, dets, label_map)
                    v2, nrel = _relabel_instances_from_labelmap(v2, label_map)   # ex1/ex2 -> printed compound label
                if label_map:
                    # PERSIST the figure's printed label<->structure map (GT-free figure truth) so the post-hoc
                    # verifier can score the agent against the IMAGE, not a possibly-mis-annotated GT, and flag
                    # GT cells that disagree with the figure. Keyed by bbox_index (-> detection -> structure).
                    v2.setdefault("_meta", {})["_figure_labels"] = {str(k): v for k, v in label_map.items()}
                    json.dump(v2, open(out_path, "w"), ensure_ascii=False, indent=1)
                    step = {"iter": it + 1, "call": "label_fix",
                            "result": (f"placed {nfix} product structures under their printed label; " if nfix else "")
                                      + (f"relabelled {nrel} instance_id(s) to printed compound numbers; " if nrel else "")
                                      + f"persisted figure label-map ({len(label_map)} boxes) for image-anchored scoring"}
                    transcript.append(step)
                    if on_step:
                        try: on_step(step)
                        except Exception: pass
        except Exception:
            pass
    # Persist the legend bindings (independent of the label-grid gate) + dedupe any leftover generic template
    # reactant the agent kept beside its substituted form. Best-effort; never breaks the run.
    if os.path.exists(out_path):
        try:
            cur = json.load(open(out_path))
            changed = False
            if legend_map:
                cur.setdefault("_meta", {})["_legend"] = legend_map
                changed = True
            n_dd = _dedup_template_reactants(cur)
            if n_dd:
                changed = True
                step = {"iter": it + 1, "call": "dedup_reactants",
                        "result": f"removed {n_dd} redundant generic template reactant(s) left beside their substituted form"}
                transcript.append(step)
                if on_step:
                    try: on_step(step)
                    except Exception: pass
            if changed:
                json.dump(cur, open(out_path, "w"), ensure_ascii=False, indent=1)  # keep the doc human-readable (was collapsing to one line)
        except Exception:
            pass
    # validity is NOT just "a file exists" -- a crashed/empty emit or a stale prior file at out_path
    # would otherwise report valid=True. Require the doc to parse AND carry at least one example or
    # template, so a genuine 0-example run is reported (and the batch runner can retry) honestly.
    valid = False
    try:
        if os.path.exists(out_path):
            _final = json.load(open(out_path))
            valid = bool(_final.get("selected_examples")) or bool(_final.get("reaction_templates"))
    except Exception:
        valid = False
    return {"out": out_path, "valid": valid,
            "tokens": spent_in + spent_out, "tokens_in": spent_in, "tokens_out": spent_out,
            "tokens_cached": spent_cached, "iters": it + 1, "transcript": transcript}


# ----------------------------------------------------------------------------- self-test (no API)
class _MockModel:
    """Replays a fixed tool-call script to verify the loop/dispatch/image-feedback plumbing
    without Vertex creds or spend. Mimics the Vertex response surface used by run()."""
    def __init__(self, script): self.script = script; self.i = 0
    def start_chat(self): return self
    def send_message(self, msg):
        from types import SimpleNamespace as NS
        step = self.script[self.i]; self.i += 1
        if step is None:
            parts = [NS(function_call=None, text="done")]
            return NS(candidates=[NS(content=NS(parts=parts))], usage_metadata=NS(prompt_token_count=10, candidates_token_count=5), text="done")
        parts = [NS(function_call=NS(name=n, args=a), text=None) for n, a in step]
        return NS(candidates=[NS(content=NS(parts=parts))], usage_metadata=NS(prompt_token_count=100, candidates_token_count=50), text=None)

def _self_test():
    import types
    # monkeypatch Part for the mock path
    mod = types.ModuleType("vertexai.preview.generative_models")
    class _Part:
        @staticmethod
        def from_function_response(name, response): return ("fr", name, response)
        @staticmethod
        def from_data(data, mime): return ("img", mime, len(data))
    mod.Part = _Part
    sys.modules["vertexai"] = types.ModuleType("vertexai")
    sys.modules["vertexai.preview"] = types.ModuleType("vertexai.preview")
    sys.modules["vertexai.preview.generative_models"] = mod
    # selftest fixture dir: override with HARNESS_SELFTEST_PAPER (the old default was an author-local path)
    paper = os.environ.get("HARNESS_SELFTEST_PAPER") or str(REPO / "examples" / "synfact_single")
    out = os.environ.get("HARNESS_SELFTEST_OUT") or str(Path(tempfile.gettempdir()) / "harness_selftest.v2.json")
    fig = int(os.environ.get("HARNESS_SELFTEST_FIG") or 1)   # examples/synfact_single ships 1.png
    plan = {"is_multistep": False,
            "_meta": {"task_id": "selftest", "paper_id": "p50", "anchor_image": f"{fig}.png",
                       "anchor_figure": "Table 2", "identifier_map": {"2": "7", "1": "14"}, "texts": []},
            "selected_examples": [{"label": "entry1", "reactants": [{"smiles": "O=C1CC[C@H]2C[C@@H]12", "identifier": "7"}],
                                    "products": [{"smiles": "COC1(OC)C(O)C[C@H]2C[C@H]21", "identifier": "14"}],
                                    "conditions": [[{"role": "oxidant", "text": "PhI(OAc)2"}]], "metrics": {"yield": "71 A%"}}],
            "reaction_templates": [], "reaction_graph": []}
    script = [
        [("read_detections", {"figure_idx": fig})],
        [("opsin", {"name": "(1R,5R)-2,2-dimethoxybicyclo[3.1.0]hexan-3-ol"})],
        [("cip", {"smiles": "COC1(OC)C(O)C[C@H]2C[C@H]21"})],
        [("crop", {"figure_idx": fig, "bbox": [720, 110, 1010, 300], "scale": 3})],
        [("emit_full", {"plan": plan, "out": out})],
        [("graph", {"v2_path": out})],
        [("finish", {"summary": "ok"})],
        None,
    ]
    res = run(paper, fig, out, _model=_MockModel(script))
    print(json.dumps({"valid": res["valid"], "iters": res["iters"], "tokens": res["tokens"],
                      "calls": [t.get("call") for t in res["transcript"] if "call" in t]}, indent=1))
    print("VERDICT:", "PASS" if res["valid"] and "finish" in [t.get("call") for t in res["transcript"]] else "FAIL")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("paper_dir", nargs="?"); ap.add_argument("figure_idx", nargs="?", type=int)
    ap.add_argument("--out", default="/tmp/harness_fig.v2.json")
    ap.add_argument("--max-iters", type=int, default=30)
    ap.add_argument("--max-tokens", type=int, default=400_000)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        _self_test()
    else:
        r = run(a.paper_dir, a.figure_idx, a.out, a.max_iters, a.max_tokens)
        print(json.dumps({k: r[k] for k in ("out", "valid", "tokens", "iters")}, indent=1))
        for t in r["transcript"]:
            print("  ", json.dumps(t)[:200])
