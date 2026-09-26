"""engine.v2 — assemble a flat PLAN into our FULL V2 schema (the format the viewer
renders) and VALIDATE it. This is what `tool.py emit` calls.

INPUT plan.json (you write this). Everything except selected_examples is optional;
each reactant/product/condition may be a plain SMILES/text string OR a dict carrying
provenance (so a fresh model can stay simple OR be fully faithful):

{
  "is_multistep": false,                       # true if the figure is a chained synthesis
  "synthesis_overview": {                      # multistep only
      "type": "linear|fan_out_derivatization|mechanistic_branched|linear_with_failures",
      "description": "verbatim what the scheme shows"},
  "selected_examples": [
    {
      "reactants": [                           # str OR {smiles, identifier, bbox, mol_id, role, type}
        "O=C(OC1CC1)c1ccccc1",
        {"smiles":"NC(C)c1ccccc1", "identifier":"amine", "bbox":[x0,y0,x1,y1], "mol_id":"p1_8", "role":"reagent"}
      ],
      "products":  ["CC(NC(=O)c1ccccc1)c1ccccc1"],
      "conditions":[                           # str (text) OR {role, text, smiles, bbox} for a DRAWN reagent
        {"role":"catalyst","text":"Pd(OAc)2","smiles":"[Pd]"}, "Selectfluor", "MeCN, 120C"],
      "metrics": {"yield":"82%","dr":"5:1","ee":"90%"},   # dict OR list of {type,content,for_product}
      "yield_text": "82%",                     # back-compat shortcut (still works)
      "label": "4l",                           # the compound/entry id (-> instance_id)
      "description": "4l, 82% yield, 5:1 d.r.", # verbatim transcript at this example (optional)
      "failed": false,                         # true ONLY for red-X / n.r. / 0% steps (NOT low yield, NOT controls)
      "markush_enumeration": {"R":"*c1ccccc1"},# the R you used, as *-rooted SMILES
      "inherit_template": "T1",                # which template this instance came from
      "_multistep": {"step_order":1, "sequence_id":"s1", "intermediate_role":"start"}
    }
  ],
  "reaction_templates": [                       # the GENERIC markush form(s); keep '*'
    {"template_id":"T1", "reactants":["*C(OC1CC1)=O<sep><a>0:R</a>"], "products":["*C(NC(*)*)=O<sep>..."],
     "conditions":["..."], "_multistep":{"step_order":1}}
  ],
  "reaction_graph": [                           # multistep DAG edges (validated against known mol-ids/labels)
    {"edge_id":"e1","step_order":1,"step_label":"Step I","from_mols":["1"],"to_mols":["2"],"via_template":"T1"}
  ]
}

OUTPUT V2 (viewer-compatible). The CRITIQUE prints VERDICT + a FIELD-COVERAGE line so you
can see which rich fields you populated. Agent-built concrete SMILES are canonicalised; accepted
MolParser E-SMILES remain byte-verbatim and must be
RDKit-valid with NO leftover '*'.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_REPO = os.environ.get("CHEM_REPO")
if not _REPO:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "tools_product/uniparser_to_v2.py").exists():
            _REPO = str(p); break
sys.path.insert(0, _REPO or "")
sys.path.insert(0, str(Path(_REPO or ".") / "tools_product"))
from uniparser_to_v2 import canon, is_markush, strip_esmiles  # noqa: E402
from tools.esmiles_audit import audit_esmiles  # noqa: E402


def _canon_esmiles(smi) -> str:
    """Canonicalize an E-SMILES while PRESERVING the <sep> multi-component boundaries and the
    trailing <a>idx:label</a> abbreviation tags. The frozen scorer maps <sep>->'.' itself
    (score_paper.norm_mol), so keeping <sep> is scoring-safe and keeps the OCSR read consistent
    (the viewer splits components on <sep>). Single-component / untagged input is unchanged from
    canon(). A non-string input yields ''."""
    if not isinstance(smi, str) or not smi:
        return "" if not isinstance(smi, str) else smi

    def _one(seg):
        # a segment may be "<SMILES><a>tag</a>..." -> canon only the SMILES head, keep tags verbatim
        i = seg.find("<a>")
        if i == -1:
            return (canon(seg, stereo=True) or seg) if seg else seg
        body, tail = seg[:i], seg[i:]
        return ((canon(body, stereo=True) or body) if body else body) + tail

    if "<sep>" not in smi:
        return _one(smi)
    return "<sep>".join(_one(seg) for seg in smi.split("<sep>"))


def _mol_entry(item, default_ident: str, concrete: bool, issues: list, tag: str):
    """item = SMILES str OR dict{smiles, identifier, bbox, mol_id, role, type, markush}."""
    if isinstance(item, dict):
        smi = item.get("smiles") or item.get("E-SMILES") or ""
        ident = item.get("identifier") or default_ident
    else:
        smi, ident = item, default_ident
    if not isinstance(smi, str):
        # a malformed molecule value (int/list/None) must invalidate ONLY this molecule,
        # never crash the whole assembly at `"*" in smi` / canon(int).
        if smi not in (None, ""):
            issues.append(f"{tag} non-string molecule ({type(smi).__name__}): {str(smi)[:40]}")
        smi = ""
    n_star = n_bad = 0
    c = canon(smi, stereo=True)
    if "*" in (smi or ""):
        n_star = 1; issues.append(f"{tag} still markush: {smi}")
    elif not c:
        n_bad = 1; issues.append(f"{tag} not RDKit-valid: {smi}")
    src = {"type": "image_structure" if concrete else "template", "agent": "skill"}
    if isinstance(item, dict) and item.get("identifier") not in (None, ""):
        # Preserve whether the extractor explicitly read/provided this identifier. Relabeling may
        # derive an instance_id, but must never overwrite a printed id (or the skill p<bbox> fallback).
        src["identifier_explicit"] = True
    if isinstance(item, dict):
        for k_in, k_out in [("bbox", "bbox"), ("bbox_index", "bbox_index"), ("mol_id", "mol_id"),
                            ("type", "type"), ("molparser_md5", "molparser_md5"),
                            ("molparser_markush", "molparser_markush"), ("markush", "markush"),
                            # The full raw cache retains caption/smi/esmi. Downstream chemistry keeps
                            # only MolParser's display/index-stable E-SMILES, verbatim.
                            ("molparser_esmi", "ocsr_esmi"), ("ocsr_esmi", "ocsr_esmi"),
                            ("ocsr_override", "ocsr_override"),    # explicit image-grounded correction {reason,evidence}
                            ("ocsr_audit", "ocsr_audit"),          # mandatory crop decision {decision,evidence}
                            ("stereo_audit", "stereo_audit"),      # mapped visible E/Z decision and RDKit verification
                            ("bbox_inherit", "bbox_inherit"),      # False for text-only/undrawn reagents; never fabricate a structure box
                            ("_scaffold_box", "_scaffold_box"),   # box is the drawn parent scaffold (shared R-series), not this cell's own
                            ("ocsr_note", "ocsr_note"), ("note", "ocsr_note"),  # model's note re: a suspected OCSR misread (kept for review)
                            ("role", "role")]:
            if item.get(k_in) is not None:
                src[k_out] = item[k_in]
    # provenance tag (drives the UI source badge). Deterministic from the signals we have:
    #   markush  = came from a markush/R-group detection (R extracted + filled in)
    #   molparser= an OCSR detection used directly (has md5/bbox_index)
    #   agent    = model-built via retro/reasoning, not drawn as its own detection
    if src.get("molparser_markush"):
        src["origin"] = "markush"
    elif src.get("molparser_md5") or src.get("bbox_index") is not None or src.get("bbox") is not None:
        src["origin"] = "molparser"
    else:
        src["origin"] = "agent"
    # Preserve MolParser E-SMILES verbatim for drawn structures. Canonicalization is useful for
    # validation/equality only; serializing it back would reorder atoms and can invalidate the
    # atom-indexed <a> tags or change how the Reviewer renders the OCSR structure. An explicit
    # image-grounded override deliberately uses the corrected model value instead.
    _raw_esmi = src.get("ocsr_esmi")
    _audit = src.get("ocsr_audit")
    _override = src.get("ocsr_override")
    _explicit_override = (
        isinstance(_audit, dict) and _audit.get("decision") == "override"
        and isinstance(_override, dict)
    )
    if isinstance(_raw_esmi, str) and _raw_esmi and not _explicit_override:
        _esm = _raw_esmi
        if canon(smi, stereo=True) != canon(_raw_esmi, stereo=True):
            src["model_esmiles"] = smi
            src["_disagree"] = True
            src["_ocsr_preferred"] = True
    else:
        _esm = _canon_esmiles(smi)
    e = {"identifier": ident, "E-SMILES": _esm, "_source": src}
    if isinstance(item, dict):
        amt = item.get("amount") or item.get("equiv") or item.get("stoichiometry")
        if amt not in (None, ""):
            e["amount"] = str(amt)            # reactant stoichiometry drawn beside it (e.g. '1.2 equiv') -- must not be dropped
    return e, n_star, n_bad


def _conditions(raw, issues=None, stats=None, tag="conditions"):
    """Normalize conditions and fail closed on unresolved concrete E-SMILES.

    A tagged wildcard body is not a concrete catalyst merely because RDKit parses its
    body. Unsupported labels/open slots remain in immutable provenance, while the
    unresolved string is withheld from the concrete ``E-SMILES`` field and counted
    against assembly validity.
    """
    if not raw:
        return [[]]
    if isinstance(raw, dict):
        raw = [raw]                                     # a single condition dict -> one flat step
    elif isinstance(raw, str):
        raw = [{"role": "reagent", "text": raw}]        # a bare string -> one reagent, not per-char
    if not isinstance(raw, list):
        return [[]]
    # each outer element is a step (list) OR a single entry (flat one-step). Mixed forms:
    # wrap any non-list element as its own step so a stray dict is never char-iterated.
    if any(isinstance(x, list) for x in raw):
        steps = [x if isinstance(x, list) else [x] for x in raw]
    else:
        steps = [raw]
    out = []
    for step in steps:
        entries = []
        for x in step:
            if isinstance(x, dict):
                txt = x.get("text", "")
                role = x.get("role", "reagent")
                ent = {"role": role if isinstance(role, str) else str(role),
                       "text": txt if isinstance(txt, str) else str(txt)}
                sm = x.get("smiles") or x.get("E-SMILES")
                provenance = {}
                raw_esmi = x.get("molparser_esmi") or x.get("ocsr_esmi")
                if isinstance(raw_esmi, str) and raw_esmi:
                    provenance["ocsr_esmi"] = raw_esmi
                if isinstance(sm, str) and sm:
                    audit = audit_esmiles(sm)
                    provenance["esmiles_audit"] = {
                        key: audit.get(key)
                        for key in (
                            "semantic_valid", "concrete_valid", "unsupported_labels",
                            "open_markush_labels", "unknown_tags", "remaining_dummies",
                            "raw_sha256",
                        )
                        if audit.get(key) is not None
                    }
                    if audit["concrete_valid"]:
                        # Accepted MolParser protocol data stays byte-verbatim; an agent-built
                        # concrete correction may be canonicalized for stable downstream use.
                        ent["E-SMILES"] = sm if raw_esmi == sm else _canon_esmiles(sm)
                    else:
                        if issues is not None:
                            issues.append(f"{tag} unresolved concrete E-SMILES withheld: {sm[:80]}")
                        if stats is not None:
                            stats["unresolved"] = stats.get("unresolved", 0) + 1
                        provenance.setdefault("ocsr_esmi", sm)
                if x.get("bbox") is not None:
                    provenance["bbox"] = x["bbox"]
                if x.get("bbox_index") is not None:
                    provenance["bbox_index"] = x["bbox_index"]
                if provenance:
                    ent["_source"] = provenance
                entries.append(ent)
            else:
                s = str(x).strip()
                if s and not s.isdigit():            # a bare integer is an index/placeholder, NOT a reagent -> drop
                    entries.append({"role": "reagent", "text": s})
        out.append(entries)
    return out


_METRIC_KEYS = ("yield", "ee", "dr", "er", "rr", "conversion", "selectivity", "yield_text")

# paper-level reaction provenance (SPEC_PAPER_LEVEL_EXTRACTION.md)
CATEGORIES = ("target_synthesis", "substrate_scope", "prior_art", "retrosynthesis",
              "mechanism", "failed", "optimization", "other")
ANCHOR_KINDS = ("figure", "scheme", "table", "text")


def _source_anchors(raw, issues, tag):
    """Normalize a reaction's paper-level source anchor(s). Accepts a single dict or a
    list of dicts. Each anchor links ONE reaction back to WHERE in the article it came
    from: a graphic region (kind in figure/scheme/table -> page + bbox_pdf) or prose
    (kind=text -> page + text_span{char_start,char_end,quote}). A reaction may carry
    several (drawn AND described). Unknown kinds are warned but kept."""
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for a in items:
        if not isinstance(a, dict):
            issues.append(f"{tag} source_anchor not a dict: {a!r}")
            continue
        kind = a.get("kind") or ("text" if a.get("text_span") else "figure")
        if kind not in ANCHOR_KINDS:
            issues.append(f"{tag} source_anchor unknown kind '{kind}'")
        anc = {"kind": kind}
        for k in ("page", "label", "bbox_pdf", "region_bbox_pdf", "region_id", "extractor"):
            if a.get(k) is not None:
                anc[k] = a[k]
        ts = a.get("text_span")
        if isinstance(ts, dict):
            anc["text_span"] = {k: ts[k] for k in ("char_start", "char_end", "page", "quote")
                                if ts.get(k) is not None}
        out.append(anc)
    return out


def _is_placeholder(pid):
    return len(pid) >= 2 and pid[0] in "pr" and pid[1:].isdigit()   # 'p0','r1' -> not a real product id


def _metrics(ex):
    """Normalize metrics into a FLAT list of GT-shape single metrics {role, of, text}.
    The model frequently crams yield+ee+dr into ONE dict — either bare ({yield,ee,dr}) or as the lone
    item of a list ([{yield,ee,dr,for_product}]) — and the old code only split the BARE form, so ee/dr
    were silently dropped. Split EITHER form so every metric (yield, ee, dr, ...) is its own entry,
    matching GT (one metric per type per product). Already-structured {role/type, text/content} pass
    through (keys normalized)."""
    m = ex.get("metrics")
    items = m if isinstance(m, list) else ([m] if isinstance(m, dict) else [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("of") or it.get("for_product") or it.get("for") or it.get("product") or "")
        # Keep an explicit p<bbox> target here: that is the skill's legitimate fallback identifier
        # for an unlabeled product. The assembler validates it against this example's product ids.
        vkeys = [k for k in it if k in _METRIC_KEYS and it[k] not in (None, "")]
        if vkeys:                                      # value-key dict (possibly multi) -> one metric per key
            for k in vkeys:
                o = {"role": "yield" if k == "yield_text" else k, "text": str(it[k])}
                if pid:
                    o["of"] = pid
                out.append(o)
        else:                                          # already a single structured metric
            role = it.get("role") or it.get("type")
            val = it.get("text") or it.get("content") or it.get("value")
            if role and val not in (None, ""):
                o = {"role": str(role), "text": str(val)}
                if pid:
                    o["of"] = pid
                if isinstance(it.get("_source"), dict):
                    o["_source"] = it["_source"]
                out.append(o)
    if not out and ex.get("yield_text"):
        out = [{"role": "yield", "text": str(ex["yield_text"])}]
    return out


_COMPOUND_LEAD = re.compile(r"^\s*(\d+[a-z]?)(?!\s*%)\b")   # leading compound number, but NOT a yield '64%'
_R_ASSIGN = re.compile(r"\b(R\d*[a-z]?)\s*=\s*([^,;]+)")


def _markush_from_desc(desc):
    """Parse the R-group assignments the model wrote in an example's description
    ('5 (R2=H)', '7 (R5=OMe)', 'R=(CH2)2OBz', 'R1=Ph, R2=Me') into {Rlabel: value}. Captures the
    association whether or not the model called the fhr fill tool -- so a directly-read OCSR cell
    still records WHICH group sits at each R position. Values may themselves contain parentheses
    (e.g. '(CH2)2OBz'), so we take everything up to a comma/semicolon and only strip a trailing ')'
    that closes an ENCLOSING group like '5 (R2=H)'."""
    out = {}
    for m in _R_ASSIGN.finditer(desc or ""):
        val = m.group(2).strip()
        while val.endswith(")") and val.count(")") > val.count("("):
            val = val[:-1].strip()                    # drop the enclosing ')' , keep balanced inner parens
        if val:
            out[m.group(1)] = val
    return out


def _first_product_box(e):
    for p in (e.get("products") or []):
        if isinstance(p, dict):
            bi = p.get("bbox_index")
            if bi is None:
                bi = (p.get("_source") or {}).get("bbox_index")
            return bi
    return None


def _cond_sig(cond):
    out = []
    for step in (cond or []):
        if not isinstance(step, list):                # a flat/raw shape must not be char-iterated here
            continue
        for c in step:
            if isinstance(c, dict):
                out.append((c.get("role"), str(c.get("text") or "").strip()))  # non-string text -> str, never crash
    return tuple(out)


def _fix_cross_template_conditions(examples, templates, issues):
    """Fix conditions that belong to the WRONG template (seen on 827: ex1-6 inherit T2 but the model
    copied T1's conditions onto them). When a figure has >=2 templates with distinct conditions and an
    example's conditions exactly match a template OTHER than the one it inherits, reset them to the
    inherited template's conditions. Only fires on this unambiguous cross-contamination. In-place."""
    tconds = {t.get("template_id"): (_cond_sig(t.get("conditions")), t.get("conditions"))
              for t in templates if t.get("template_id")}
    if len(tconds) < 2:
        return
    for e in examples:
        tid = e.get("inherit_template")
        own = tconds.get(tid)
        if not own or not own[0]:
            continue
        sig = _cond_sig(e.get("conditions"))
        if not sig or sig == own[0]:
            continue                                              # empty or already this template's conditions
        if any(sig == s for k, (s, _) in tconds.items() if k != tid):  # matches a DIFFERENT template -> contaminated
            e["conditions"] = [list(step) for step in (own[1] or [])]
            issues.append(f"ex conditions matched a different template; reset to inherited {tid}")


def _recover_copied_products(examples, issues):
    """Fix the 'model emitted the reactant as the product' copy error (seen on 656 box12): when a
    product's structure is identical to one of the example's own reactants, and that product's OCSR
    detection read a DIFFERENT valid structure (the actual drawn product), replace the product
    E-SMILES with the OCSR read. Only triggers on the unambiguous copy case -- OCSR already saw the
    right structure, our assembly just kept the model's wrong (reactant) copy. In-place."""
    for e in examples:
        r_keys = set()
        for r in (e.get("reactants") or []):
            c = canon(r.get("E-SMILES") if isinstance(r, dict) else r, stereo=True)   # EXACT (with stereo)
            if c:
                r_keys.add(c)
        if not r_keys:
            continue
        for p in (e.get("products") or []):
            if not isinstance(p, dict):
                continue
            pk = canon(p.get("E-SMILES"), stereo=True)
            if not pk or pk not in r_keys:
                continue                                          # product is an EXACT copy of a reactant (incl. stereo)
            # -- NOT an isomerization/epimerization/E-Z-inversion/recovered-SM (those differ in stereo, so pk != r_key)
            ocsr = (p.get("_source") or {}).get("ocsr_esmi")
            oc = canon(ocsr, stereo=True) if (ocsr and "*" not in str(ocsr)) else None  # reject ANY leftover '*'
            if oc and oc not in r_keys:                           # OCSR saw a different, concrete structure -> trust it
                p["E-SMILES"] = ocsr
                p["_source"]["_recovered"] = "product duplicated a reactant -> restored from OCSR read"
                issues.append("ex product duplicated a reactant; recovered the drawn product from OCSR")


def _prefer_ocsr_for_concrete(examples, issues):
    """Concrete short-circuit (2026-07-22, Guard A1): when a product's OCSR detection read a
    CONCRETE structure (molparser_markush is False, no leftover '*'), trust MolParser's read as
    the authoritative E-SMILES. The model tends to RE-TYPE drawn structures with transcription
    errors -- the agent is NET-NEGATIVE on OCSR (MolParser correct ~77% vs agent-final ~76%),
    and on scope tables it re-derives R-groups by eye instead of using the correct drawn read
    (e.g. 03e9c566 cell 39: MolParser read cyclobutane+phenethyl-boronate correctly, the model
    hand-wrote a wrong benzyl+methyl product). MolParser is the default here; the model's
    disagreeing read is kept on _source.model_esmiles (+ _disagree). An extractor may override only
    by supplying a corrected SMILES plus a non-empty `ocsr_override:{reason,evidence}` grounded in
    explicit image evidence; the immutable raw read remains in _source.ocsr_esmi. In-place."""
    for e in examples:
        for p in (e.get("reactants") or []) + (e.get("products") or []):
            if not isinstance(p, dict):
                continue
            src = p.get("_source") or {}
            if src.get("molparser_markush"):                  # a markush/template read, not a concrete drawn cell
                continue
            ocsr = src.get("ocsr_esmi")
            if not ocsr or "*" in str(ocsr):                  # need a concrete OCSR read (no R placeholders)
                continue
            oc = canon(ocsr, stereo=True)
            if not oc:
                continue
            cur = canon(p.get("E-SMILES"), stereo=True)
            if cur == oc:
                continue                                       # E-SMILES already equals the OCSR read -> nothing to do
            override = src.get("ocsr_override")
            audit = src.get("ocsr_audit")
            override_valid = (
                isinstance(audit, dict)
                and audit.get("decision") == "override"
                and isinstance(audit.get("evidence"), str)
                and bool(audit["evidence"].strip())
                and isinstance(override, dict)
                and isinstance(override.get("reason"), str)
                and bool(override["reason"].strip())
                and isinstance(override.get("evidence"), str)
                and bool(override["evidence"].strip())
                and cur is not None
            )
            if override_valid:
                # Raw MolParser remains in _source.ocsr_esmi. The corrected E-SMILES is accepted only
                # through this explicit, auditable image-evidence lane; a bare model disagreement is
                # still reverted to OCSR by the default guard below.
                src["_ocsr_override_applied"] = True
                p["_source"] = src
                issues.append(
                    f"product {p.get('identifier')}: accepted image-grounded override of concrete OCSR read"
                )
                continue
            if cur is not None:                                # keep the model's disagreeing read for later review
                src["model_esmiles"] = p.get("E-SMILES")
                src["_disagree"] = True
            p["E-SMILES"] = ocsr
            src["_ocsr_preferred"] = True
            p["_source"] = src
            issues.append(f"product {p.get('identifier')}: trusted concrete OCSR read over model re-type")


def _has_potential_ez(smiles):
    """Whether a concrete molecule contains at least one potentially stereogenic C=C bond."""
    try:
        from rdkit import Chem
        molecule = Chem.MolFromSmiles(strip_esmiles(str(smiles or "")))
        if molecule is None:
            return False
        return any(str(info.type) == "Bond_Double" for info in Chem.FindPotentialStereo(molecule))
    except Exception:
        return False


def _element_symbols(smiles):
    try:
        from rdkit import Chem
        molecule = Chem.MolFromSmiles(strip_esmiles(str(smiles or "")))
        if molecule is None:
            return set()
        return {atom.GetSymbol() for atom in molecule.GetAtoms() if atom.GetAtomicNum() > 1}
    except Exception:
        return set()


def _count_missing_atom_sources(examples, issues):
    """Products cannot introduce an element absent from every recorded reactant."""
    missing = 0
    for example in examples:
        reactant_elements = set().union(*(
            _element_symbols(molecule.get("E-SMILES"))
            for molecule in example.get("reactants") or []
            if isinstance(molecule, dict)
        ))
        product_elements = set().union(*(
            _element_symbols(molecule.get("E-SMILES"))
            for molecule in example.get("products") or []
            if isinstance(molecule, dict)
        ))
        absent = sorted(product_elements - reactant_elements)
        if absent:
            missing += 1
            issues.append(
                f"example {example.get('instance_id')}: product elements lack reactant source: {','.join(absent)}"
            )
    return missing


def _rdkit_ez_labels(smiles):
    try:
        from rdkit import Chem
        molecule = Chem.MolFromSmiles(strip_esmiles(str(smiles or "")))
        if molecule is None:
            return set()
        Chem.AssignStereochemistry(molecule, cleanIt=True, force=True)
        labels = set()
        for bond in molecule.GetBonds():
            if bond.GetBondType() != Chem.BondType.DOUBLE:
                continue
            if bond.GetStereo() == Chem.BondStereo.STEREOE:
                labels.add("E")
            elif bond.GetStereo() == Chem.BondStereo.STEREOZ:
                labels.add("Z")
        return labels
    except Exception:
        return set()


def _count_missing_stereo_audits(examples, issues):
    """Fail closed until every potentially stereogenic alkene has a consistent mapped audit."""
    missing = 0
    for example in examples:
        for molecule in (example.get("reactants") or []) + (example.get("products") or []):
            if not isinstance(molecule, dict) or not _has_potential_ez(molecule.get("E-SMILES")):
                continue
            audit = (molecule.get("_source") or {}).get("stereo_audit")
            valid = (
                isinstance(audit, dict)
                and audit.get("decision") in {"specified", "unspecified", "ambiguous"}
                and isinstance(audit.get("evidence"), str)
                and bool(audit["evidence"].strip())
            )
            if audit and audit.get("decision") == "specified":
                valid = valid and audit.get("rdkit_ez") in {"E", "Z"}
            if valid and audit.get("decision") == "specified":
                actual = _rdkit_ez_labels(molecule.get("E-SMILES"))
                if audit.get("rdkit_ez") not in actual:
                    valid = False
                    issues.append(
                        f"molecule {molecule.get('identifier')}: stereo audit says {audit.get('rdkit_ez')} but final SMILES has {','.join(sorted(actual)) or 'unspecified'}"
                    )
            if not valid:
                missing += 1
                if not (audit and audit.get("decision") == "specified"):
                    issues.append(
                        f"molecule {molecule.get('identifier')}: missing mapped alkene stereo audit"
                    )
    return missing


def _count_missing_ocsr_audits(examples, issues):
    """Require a per-crop accept/override decision whenever raw concrete OCSR is attached."""
    missing = 0
    for example in examples:
        for molecule in (example.get("reactants") or []) + (example.get("products") or []):
            if not isinstance(molecule, dict):
                continue
            src = molecule.get("_source") or {}
            if not src.get("ocsr_esmi") or src.get("molparser_markush"):
                continue
            audit = src.get("ocsr_audit")
            valid = (
                isinstance(audit, dict)
                and audit.get("decision") in {"accepted", "override"}
                and isinstance(audit.get("evidence"), str)
                and bool(audit["evidence"].strip())
            )
            if not valid:
                missing += 1
                issues.append(
                    f"molecule {molecule.get('identifier')}: missing concrete OCSR crop audit"
                )
    return missing


def _relabel_examples_by_description(examples):
    """Give each example a real compound-number instance_id when the model left it blank (so the
    engine had fallen back to ex1/ex2). Two signals combined, deterministically (no image read):
      (a) the compound number in the example's description leading token ('5 (R2=H)' -> '5');
      (b) examples whose product is the SAME drawn box are R-variants of ONE compound, so a number
          found on ANY member is propagated to the whole box-group -- this recovers the label even
          when the model wrote it on only some rows.
    A number shared by several examples gets an a/b/c suffix -> 5a,5b,5c. Only ex{i}-fallback ids
    are rewritten; a real printed label already set is left untouched. In-place."""
    from collections import OrderedDict
    fb = [i for i, e in enumerate(examples) if re.fullmatch(r"ex\d+", str(e.get("instance_id") or ""))]
    if not fb:
        return

    # a real printed compound number (39, 13a) -- not a default ident (p0/r0), ex-fallback, or yield
    _CN = re.compile(r"^\d+[a-z]?$")

    def num_of(i):
        # Prefer any extractor-provided product identifier, including stereochemical printed labels
        # such as "(R)-1". Only generated defaults (p0/p1/...) and yield-like strings are excluded.
        for p in (examples[i].get("products") or []):
            if isinstance(p, dict):
                pid = str(p.get("identifier") or "").strip()
                explicit = bool((p.get("_source") or {}).get("identifier_explicit"))
                if explicit and pid and not _is_placeholder(pid) and not re.fullmatch(r"\d+(?:\.\d+)?%", pid):
                    return pid
                if _CN.match(pid):
                    return pid
                break
        m = _COMPOUND_LEAD.match(examples[i].get("description") or "")
        return m.group(1) if m else None

    # group fallback examples: variants of one compound share their product's drawn box; else per-example
    groups = OrderedDict()
    for i in fb:
        box = _first_product_box(examples[i])
        key = ("box", box) if box is not None else ("row", i)
        groups.setdefault(key, []).append(i)
    # ids already committed on non-fallback examples: a box-derived id for an unlabeled cell must
    # never collide with a printed compound number set elsewhere (or on an earlier relabel pass).
    fb_set = set(fb)
    taken = {str(e.get("instance_id")) for j, e in enumerate(examples) if j not in fb_set}
    base_used = {}                                                 # base -> next suffix position (global dedup)
    for key, idxs in groups.items():
        base = next((num_of(i) for i in idxs if num_of(i)), None)  # a number on ANY member labels the whole group
        id_src = "description"
        if not base and key[0] == "box" and key[1] is not None:
            base, id_src = str(key[1]), "bbox"                     # no printed number -> use the drawn box id (5a,5b,5c)
        if not base:
            continue                                               # not even a box -> leave ex-fallback
        base_has_suffix = bool(base) and base[-1].isalpha()       # e.g. base '5a' -> don't append another letter
        multi = (base_used.get(base, 0) + len(idxs)) > 1
        pos = base_used.get(base, 0)
        for i in idxs:
            while True:                                            # bump the suffix until the candidate id is globally unique
                if not multi and pos == 0:
                    cand = base
                elif base_has_suffix or pos >= 26:                # already lettered, or past 'z' -> numeric suffix stays unique
                    cand = f"{base}_{pos + 1}"
                else:
                    cand = f"{base}{chr(97 + pos)}"
                pos += 1
                if cand not in taken:
                    break
            taken.add(cand)
            examples[i]["instance_id"] = cand
            examples[i]["_id_source"] = id_src                     # provenance: printed-label vs box-derived id
            prods = [p for p in (examples[i].get("products") or []) if isinstance(p, dict)]
            if prods:
                old_pid = prods[0].get("identifier")
                explicit = bool((prods[0].get("_source") or {}).get("identifier_explicit"))
                if not explicit:
                    prods[0]["identifier"] = cand                  # generated id only; never overwrite an extractor-read id
                    for m in (examples[i].get("metrics") or []):   # keep links valid only when the product id changed
                        if isinstance(m, dict) and str(m.get("of")) == str(old_pid):
                            m["of"] = cand
        base_used[base] = pos


def _resolve_inherit(inh, tmpl_ids):
    """Normalize a model-supplied inherit_template into a real template_id (T1/T2...).
    The model emits this field as an int index, a bare digit, "T1", or garbage (bool/None)
    -- survey showed only ~1% used the correct "T1" form. Map the recoverable ones onto the
    assembled template ids; drop the unrecognizable rather than storing a wrong-typed value."""
    if inh is None or isinstance(inh, bool):
        return None
    if isinstance(inh, int):
        return tmpl_ids[inh] if 0 <= inh < len(tmpl_ids) else None
    s = str(inh).strip()
    if s in tmpl_ids:
        return s
    u = s.upper()
    if u[:1] == "T" and u[1:].isdigit():
        n = int(u[1:])
        return tmpl_ids[n - 1] if 1 <= n <= len(tmpl_ids) else None   # 'T1' -> the ACTUAL first id (handles custom ids)
    if s.isdigit():
        idx = int(s)
        return tmpl_ids[idx] if 0 <= idx < len(tmpl_ids) else None     # bare digit = 0-based index (the model's convention)
    return None


def _inherit_bbox(examples, templates):
    """Every molecule must carry a box for indexing + the reviewer overlay. A markush-enumerated
    or R-filled cell has no detection box of its own -> inherit the corresponding TEMPLATE
    molecule's box (通式的框) via inherit_template (positional, by role), then fall back to any
    boxed sibling in the same example (实施例的框). Never overwrites a real detection box; tags
    `_bbox_inherited` so provenance stays honest."""
    tmpl_by_id = {t.get("template_id"): t for t in templates if isinstance(t, dict)}
    sole = templates[0] if len(templates) == 1 else None

    def has_box(m):
        s = (m.get("_source") or {}) if isinstance(m, dict) else {}
        return s.get("bbox") is not None or s.get("bbox_index") is not None

    def donate(dst, src_mol, kind):
        s = (src_mol.get("_source") or {}) if isinstance(src_mol, dict) else {}
        d = dst.setdefault("_source", {})
        if s.get("bbox") is not None:
            d["bbox"] = s["bbox"]
        if s.get("bbox_index") is not None:
            d.setdefault("bbox_index", s["bbox_index"])
        # carry the donor's OCSR read so the annotator sees it in place (no navigating back to the
        # template); only when the target has none of its own.
        if s.get("ocsr_esmi") and not d.get("ocsr_esmi"):
            d["ocsr_esmi"] = s["ocsr_esmi"]
        d["_bbox_inherited"] = kind                # "template" (通式) or "example_region" (实施例) -- honest, not a real depiction

    for e in examples:
        tid = e.get("inherit_template")
        tmpl = tmpl_by_id.get(tid) or sole
        for role in ("reactants", "products"):
            emols = e.get(role) or []
            tmols = (tmpl.get(role) or []) if isinstance(tmpl, dict) else []
            boxed_sib = next((m for m in (e.get("products") or []) + (e.get("reactants") or [])
                              if isinstance(m, dict) and has_box(m)), None)
            for k, m in enumerate(emols):
                if not isinstance(m, dict) or has_box(m):
                    continue
                if (m.get("_source") or {}).get("bbox_inherit") is False:
                    continue
                t = tmols[k] if k < len(tmols) else (tmols[0] if tmols else None)
                if isinstance(t, dict) and has_box(t):
                    donate(m, t, "template")            # 通式的框
                elif boxed_sib is not None:
                    donate(m, boxed_sib, "example_region")   # 实施例的框 (same-cell region, not this molecule itself)


def assemble_and_validate(plan: dict):
    examples, issues = [], []
    n_bad = n_star = 0
    condition_stats = {"unresolved": 0}
    known_ids = set()
    # every reaction_template gets a stable id T1,T2,... (the model leaves template_id unset ~99.8%
    # of the time, so inherit_template has nothing to point at). Compute up front so examples can
    # normalize their inherit_template against these ids.
    _all_tmpls = plan.get("reaction_templates") or []
    _raw_tmpls = [t for t in _all_tmpls if isinstance(t, dict)]   # drop malformed FIRST so ids stay contiguous+aligned
    if len(_raw_tmpls) != len(_all_tmpls):
        issues.append(f"{len(_all_tmpls) - len(_raw_tmpls)} reaction_template(s) were not dicts -- skipped")
    tmpl_ids = [(t.get("template_id") or f"T{k + 1}") for k, t in enumerate(_raw_tmpls)]
    _sel = plan.get("selected_examples") or []
    if not isinstance(_sel, list):                    # a dict/str here would iterate keys/chars as "examples"
        issues.append(f"selected_examples is not a list ({type(_sel).__name__}) -- none emitted")
        _sel = []
    for i, ex in enumerate(_sel):
        if not isinstance(ex, dict):
            # a malformed example (bare str / int) must NOT crash the whole emit and lose every other
            # example -- skip it with a logged issue so the rest of the plan still emits.
            issues.append(f"ex{i} is not a dict ({type(ex).__name__}: {str(ex)[:40]}) -- skipped")
            continue
        r_entries, p_entries = [], []
        for j, s in enumerate(ex.get("reactants") or []):
            e, ns, nb = _mol_entry(s, f"r{j}", True, issues, f"ex{i} reactant[{j}]")
            n_star += ns; n_bad += nb; r_entries.append(e); known_ids.add(e["identifier"])
        for j, s in enumerate(ex.get("products") or []):
            e, ns, nb = _mol_entry(s, f"p{j}", True, issues, f"ex{i} product[{j}]")
            n_star += ns; n_bad += nb; p_entries.append(e); known_ids.add(e["identifier"])
        e = {"instance_id": ex.get("instance_id") or ex.get("identifier") or ex.get("label") or f"ex{i+1}",
             "reactants": r_entries, "products": p_entries,
             "conditions": _conditions(
                 ex.get("conditions"), issues, condition_stats, f"ex{i} conditions",
             )}
        if ex.get("description"):
            e["description"] = ex["description"]
        inh = _resolve_inherit(ex.get("inherit_template"), tmpl_ids)
        if inh is None and len(tmpl_ids) == 1:
            inh = tmpl_ids[0]                 # single template on the figure -> unambiguous parent
        if inh:
            e["inherit_template"] = inh
        # R attribution: prefer the model's explicit R_map (from fhr reverse/fill), else recover it
        # from the R= assignments in the description so a directly-read cell still records its R-values.
        me = ex.get("markush_enumeration") or _markush_from_desc(ex.get("description"))
        if me:
            e["markush_enumeration"] = me
        if ex.get("category"):
            cat = str(ex["category"])
            if cat not in CATEGORIES:
                issues.append(f"ex{i} unknown category '{cat}'")
            e["category"] = cat
        ancs = _source_anchors(ex.get("source_anchors") or ex.get("source_anchor"), issues, f"ex{i}")
        if ancs:
            e["source_anchors"] = ancs
        if ex.get("optimization_runs"):                # condition-screen table -> 1 reaction + N runs
            runs = []
            for r in ex["optimization_runs"]:
                if isinstance(r, dict):
                    runs.append({k: r[k] for k in ("entry", "conditions", "metrics", "note")
                                 if r.get(k) is not None})
            if runs:
                e["optimization_runs"] = runs
        mets = _metrics(ex)
        if mets:
            # A metric without `of` is unambiguous only for a single-product reaction. For a
            # multi-product row it may be a combined yield; never attach it to the first product.
            product_ids = {str(pp.get("identifier")) for pp in p_entries}
            for mm in mets:
                target = str(mm.get("of") or "")
                if target and _is_placeholder(target) and target not in product_ids:
                    mm.pop("of", None)                 # stale generated target, not this row's product
                if len(p_entries) == 1:
                    mm.setdefault("of", p_entries[0]["identifier"])
            e["metrics"] = mets
        if ex.get("failed"):
            e["_outcome"] = {"is_failed": True}
        if ex.get("_multistep"):
            e["_multistep"] = ex["_multistep"]
        if ex.get("label"):
            known_ids.add(str(ex["label"]))
        examples.append(e)

    _recover_copied_products(examples, issues)   # fix 'product == reactant' copy error from the OCSR read
    _prefer_ocsr_for_concrete(examples, issues)  # Guard A1: trust OCSR unless an explicit image-evidence override is recorded
    n_audit_missing = _count_missing_ocsr_audits(examples, issues)
    n_stereo_audit_missing = _count_missing_stereo_audits(examples, issues)
    n_atom_source_missing = _count_missing_atom_sources(examples, issues)
    _relabel_examples_by_description(examples)   # ex1/ex2 -> compound number from description (5a,5b,...)

    templates = []
    for ti, t in enumerate(_raw_tmpls):
        if not isinstance(t, dict):
            issues.append(f"reaction_template is not a dict ({type(t).__name__}) -- skipped")
            continue
        tr_e, tp_e = [], []
        for j, s in enumerate(t.get("reactants") or []):
            me_, ns_, nb_ = _mol_entry(s, f"r{j}", False, issues, f"tmpl reactant[{j}]")
            tr_e.append(me_); n_bad += nb_          # a truly invalid template SMILES counts against the verdict
            known_ids.add(me_["identifier"])
        for j, s in enumerate(t.get("products") or []):  # (markush '*' in a template is expected -> ns_ ignored)
            me_, ns_, nb_ = _mol_entry(s, f"p{j}", False, issues, f"tmpl product[{j}]")
            tp_e.append(me_); n_bad += nb_
            known_ids.add(me_["identifier"])
        tt = {"template_id": tmpl_ids[ti],       # always T1,T2,... so inherit_template can reference it
              "reactants": tr_e, "products": tp_e,
              "conditions": _conditions(
                  t.get("conditions"), issues, condition_stats, f"template {tmpl_ids[ti]} conditions",
              )}
        if t.get("_multistep"):
            tt["_multistep"] = t["_multistep"]
        templates.append(tt)

    _fix_cross_template_conditions(examples, templates, issues)   # reset conditions copied from the wrong template
    _inherit_bbox(examples, templates)                            # markush/filled cells inherit the template/example box
    _relabel_examples_by_description(examples)                     # 2nd pass: an ex-N cell now carries an (inherited) box -> box-derived id (no more bare ex1/ex2)

    # validate the reaction_graph references known mol-ids / labels
    graph = [g for g in (plan.get("reaction_graph") or []) if isinstance(g, dict)]
    if graph != (plan.get("reaction_graph") or []):
        issues.append("reaction_graph had non-dict edge(s) -- skipped")
    graph_warn = []
    for g in graph:
        for side in ("from_mols", "to_mols"):
            for mid in (g.get(side) or []):
                if str(mid) not in {str(x) for x in known_ids} and not g.get("_unverified"):
                    graph_warn.append(f"edge {g.get('edge_id','?')} {side} '{mid}' not a known mol-id/label")

    topology_audit = plan.get("topology_audit")
    n_topology_missing = 0
    if bool(plan.get("is_multistep")) and not graph:
        n_topology_missing += 1
        issues.append("is_multistep=true but reaction_graph is empty")
    if len(templates) > 1 and not graph:
        valid_independent_audit = (
            isinstance(topology_audit, dict)
            and isinstance(topology_audit.get("visible_arrows"), list)
            and topology_audit.get("visible_arrows")
            and topology_audit.get("product_reused") is False
            and isinstance(topology_audit.get("evidence"), str)
            and topology_audit.get("evidence").strip()
        )
        if not valid_independent_audit:
            n_topology_missing += 1
            issues.append(
                "multiple templates with no graph require a topology_audit proving "
                "the visible arrows are independent"
            )

    # Order: reaction_templates BEFORE selected_examples -- the templates (with markush R placeholders +
    # their verbatim OCSR E-SMILES <sep>/<a>label</a> in _source) are the schema the examples instantiate, so they
    # read first. (User-requested output ordering.)
    v2 = {"is_multistep": bool(plan.get("is_multistep")),
          "has_reaction_template": len(templates) > 0,
          "reaction_templates": templates,
          "selected_examples": examples,
          "reaction_graph": graph,
          "_extractor": "chem_extract_skill"}
    if plan.get("synthesis_overview"):
        v2["synthesis_overview"] = plan["synthesis_overview"]
    if isinstance(topology_audit, dict):
        v2["topology_audit"] = topology_audit

    # ---- verdict + field-coverage report ----
    n = len(examples)
    verdict = "VALID" if (
        n_bad == 0 and n_star == 0 and n_audit_missing == 0
        and n_stereo_audit_missing == 0 and n_atom_source_missing == 0
        and condition_stats["unresolved"] == 0 and n_topology_missing == 0 and n > 0
    ) else "INVALID"
    has_cond = any(any(step for step in e["conditions"]) for e in examples)
    has_cond_smi = any("E-SMILES" in c for e in examples for step in e["conditions"] for c in step)
    has_metrics = any("metrics" in e for e in examples)
    has_bbox = any("bbox" in m["_source"] for e in examples for m in e["reactants"] + e["products"])
    has_desc = any("description" in e for e in examples)
    has_graph = bool(graph)
    has_anchor = any("source_anchors" in e for e in examples)
    has_cat = any("category" in e for e in examples)
    cov = (f"  FIELD COVERAGE: conditions={'Y' if has_cond else 'n'} "
           f"drawn-reagent-smiles={'Y' if has_cond_smi else 'n'} metrics={'Y' if has_metrics else 'n'} "
           f"provenance-bbox={'Y' if has_bbox else 'n'} description={'Y' if has_desc else 'n'} "
           f"multistep-graph={'Y' if has_graph else 'n'} "
           f"source-anchor={'Y' if has_anchor else 'n'} category={'Y' if has_cat else 'n'}")
    lines = [f"VERDICT: {verdict}",
             f"  examples={n}  templates={len(templates)}  invalid_smiles={n_bad}  leftover_markush={n_star}  missing_ocsr_audits={n_audit_missing}  missing_stereo_audits={n_stereo_audit_missing}  missing_atom_sources={n_atom_source_missing}  unresolved_condition_esmiles={condition_stats['unresolved']}  missing_topology_audit={n_topology_missing}",
             cov]
    if n == 0:
        lines.append("  WARNING: zero examples — a figure with drawn products must have >=1 example.")
    if bool(plan.get("is_multistep")) and not has_graph:
        lines.append("  WARNING: is_multistep=true but reaction_graph is empty — add the DAG edges.")
    for w in graph_warn[:6]:
        lines.append(f"  GRAPH: {w}")
    for s in issues[:12]:
        lines.append(f"  - {s}")
    if len(issues) > 12:
        lines.append(f"  ... +{len(issues)-12} more")
    return v2, "\n".join(lines)
