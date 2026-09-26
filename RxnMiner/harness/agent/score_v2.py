#!/usr/bin/env python3
"""score_v2 — the SkillOpt VALIDATION GATE r(s) for chemical-reaction V2 extraction.

Not SMILES-only. Scores EVERY important V2 field a prediction can get wrong, into one
r(s) in [0,1]. Multiplicative validity gate x 8-dim content score:

  G = 1.0 if emit_full-valid else 0.10          # no '*', all example SMILES RDKit-valid, >=1 example
  r(s) = G * sum_over_applicable( weight_i * subscore_i ) / sum(weight_i)

  dim            w     what it scores
  structures   0.30    reaction-level F1 (nostereo, direction-aware: reactant-set vs product-set)
  completeness 0.12    0.5*count_recall + 0.5*arity_ok  ("all N present, each >=1 reactant & >=1 product/failed")
  conditions   0.16    0.4*structure(list-of-list; multistep substep count) + 0.6*role-aware content F1
                       -- POOLS reaction_templates (scope-grid conditions live in the template, not the example)
  topology     0.12    0.3*is_multistep flag + 0.7*reaction_graph edge-F1 (by from/to mol-sets, not edge count)
  metrics      0.10    yield/ee/dr value F1 (role + numeric value within +-2)
  stereo       0.10    1 - clip(F1_nostereo - F1_stereo)   (penalise lost/flipped stereochemistry)
  identifiers  0.06    identifier_map hit-rate (0.7) + reaction_class soft match (0.3)
  failed       0.04    failed-row set F1 (ND/complex/unsuccessful flagged correctly)

A dimension whose GT field is absent is marked N/A and dropped, and the remaining weights
are renormalised, so the score is fair across the flat GT (`*.reviewed.json`) and the harness
_meta output.

Reuses tools_product/score_paper.py for SMILES canon + reaction matching.

Usage:
    python agent/score_v2.py <pred.json> <gt.json>            # one figure, prints breakdown + r
    python agent/score_v2.py <pred.json> <gt.json> --json     # machine-readable
    python agent/score_v2.py --paper <paper_dir> --pred-suffix _output.v2_skill_extract.json
                                                              # all figures in a paper vs *.reviewed.json
"""
from __future__ import annotations
import argparse, glob, json, re, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# reuse the canon + reaction-matching that score_paper already implements
from tools_product.score_paper import norm_mol, reactions_of, match, jaccard  # noqa: E402

try:
    from rdkit import Chem
    from rdkit import RDLogger
    RDLogger.DisableLog("rdApp.*")
except Exception:
    Chem = None

WEIGHTS = {
    "structures": 0.30, "completeness": 0.12, "conditions": 0.16, "topology": 0.12,
    "metrics": 0.10, "stereo": 0.10, "identifiers": 0.06, "failed": 0.04,
}
GATE_FAIL = 0.10  # multiplier when emit_full-invalid


# ----------------------------------------------------------------------- helpers
def _f1(matched, n_pred, n_gt):
    rec = matched / n_gt if n_gt else (1.0 if n_pred == 0 else 0.0)
    prec = matched / n_pred if n_pred else (1.0 if n_gt == 0 else 0.0)
    return (2 * rec * prec / (rec + prec)) if (rec + prec) else 0.0


def _max_match(gt_items, pred_items, compat):
    """Maximum-cardinality bipartite matching count between gt and pred under a boolean compat(g,p).
    Hungarian assignment (scipy) — correct under fuzzy/role-equivalence matching where greedy first-match
    UNDER-counts (a pred greedily consumed by an early gt can starve a later gt that only it matches).
    Falls back to greedy if scipy is unavailable."""
    n, m = len(gt_items), len(pred_items)
    if n == 0 or m == 0:
        return 0
    try:
        import numpy as np
        from scipy.optimize import linear_sum_assignment
        cost = np.ones((n, m), dtype=float)
        for i, g in enumerate(gt_items):
            for j, p in enumerate(pred_items):
                if compat(g, p):
                    cost[i][j] = 0.0
        ri, ci = linear_sum_assignment(cost)
        return int(sum(1 for i, j in zip(ri, ci) if cost[i][j] == 0.0))
    except Exception:
        used = [False] * m
        matched = 0
        for g in gt_items:
            for j, p in enumerate(pred_items):
                if not used[j] and compat(g, p):
                    used[j] = True
                    matched += 1
                    break
        return matched

_ROLE_ALIASES = {
    "catalysts": "catalyst", "reagents": "reagent", "solvents": "solvent",
    "bases": "base", "ligands": "ligand", "acids": "acid", "oxidants": "oxidant",
    "reductants": "reductant", "temperatures": "temperature", "temp": "temperature",
    "times": "time", "atmospheres": "atmosphere", "additives": "additive",
}
def _norm_role(r):
    r = (r or "").strip().lower()
    return _ROLE_ALIASES.get(r, r)

# Role EQUIVALENCE classes for condition matching. Annotators and models name the same chemical role
# differently (photocatalyst IS a catalyst; a reductant/oxidant IS a reagent; light/temperature/time/
# atmosphere are reaction parameters). Matching on the canonical bucket — precedented by RxnScribe's
# reactant<->reagent merge — credits correct chemistry that only differs in role label. Wildcard roles
# (GT's catch-all 'others', a model's generic 'conditions'/'condition') match any role on key alone.
_ROLE_CANON = {
    "catalyst": "catalyst", "photocatalyst": "catalyst", "precatalyst": "catalyst",
    "cocatalyst": "catalyst", "co-catalyst": "catalyst", "organocatalyst": "catalyst",
    "metal": "catalyst", "metal catalyst": "catalyst",
    "reagent": "reagent", "reductant": "reagent", "oxidant": "reagent", "nucleophile": "reagent",
    "electrophile": "reagent", "coupling partner": "reagent", "substrate": "reagent",
    "ligand": "ligand", "base": "base", "acid": "acid",
    "additive": "additive", "promoter": "additive", "activator": "additive",
    "solvent": "solvent", "cosolvent": "solvent", "co-solvent": "solvent",
    "light": "light", "irradiation": "light", "photo": "light", "hv": "light", "wavelength": "light",
    "temperature": "temperature", "heat": "temperature",
    "time": "time", "duration": "time",
    "atmosphere": "atmosphere", "gas": "atmosphere",
}
_WILDCARD_ROLES = {"", "conditions", "condition", "other", "others", "misc", "general", "note"}

def _role_bucket(r):
    return _ROLE_CANON.get(_norm_role(r), _norm_role(r))

def _role_compat(gr, pr):
    if _norm_role(gr) in _WILDCARD_ROLES or _norm_role(pr) in _WILDCARD_ROLES:
        return True
    return _role_bucket(gr) == _role_bucket(pr)

def _norm_text(t):
    t = (t or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = t.replace("°c", "c").replace("º", "").replace("℃", "c")
    return t.strip(" .,;")

_NUM = re.compile(r"-?\d+\.?\d*")
def _parse_num(t):
    m = _NUM.search(t or "")
    return float(m.group()) if m else None


def _examples(v2):
    return v2.get("selected_examples") or []

def _templates(v2):
    return v2.get("reaction_templates") or []

def _is_multistep(v2):
    return bool(v2.get("is_multistep_synthesis", v2.get("is_multistep", False)))

def _smiles_of(comp, stereo=False):
    return norm_mol(comp.get("E-SMILES") or comp.get("smiles") or "", stereo)


# ----------------------------------------------------------------------- GATE
def gate_valid(v2) -> bool:
    """emit_full validity: >=1 example, every concrete example SMILES RDKit-valid and '*'-free.
    Templates may legitimately carry Markush '*' and are excluded from the '*' check."""
    exs = _examples(v2)
    if not exs:
        return False
    for e in exs:
        for side in ("reactants", "products"):
            for c in e.get(side) or []:
                s = _smiles_of(c, stereo=True)
                if not s:
                    continue  # empty (e.g. failed product) -> not a validity error
                if "*" in s:
                    return False
                if Chem is not None and Chem.MolFromSmiles(s) is None:
                    return False
    return True


# ----------------------------------------------------------------------- structures + stereo
def _structures_and_stereo(pred, gt):
    out = {}
    for mode, st in (("nostereo", False), ("stereo", True)):
        pr, gr = reactions_of(pred, st), reactions_of(gt, st)
        m, _ = match(pr, gr)
        out[mode] = _f1(m, len(pr), len(gr))
    structures = out["nostereo"]
    stereo = 1.0 - max(0.0, min(1.0, out["nostereo"] - out["stereo"]))
    return structures, stereo, out


# ------------------------------------------------------ molecule-level SMILES accuracy (RDKit)
def _all_mols(v2, stereo):
    """Every reactant+product SMILES across examples+templates, RDKit-canonicalized (multiset).
    '*'-bearing Markush placeholders are skipped (only concrete molecules counted)."""
    out = []
    for e in (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or []):
        for side in ("reactants", "products"):
            for c in e.get(side) or []:
                s = _smiles_of(c, stereo)
                if s and "*" not in s:
                    out.append(s)
    return out


def _role_mols(v2, side, stereo):
    """concrete (no-'*') canonical SMILES on ONE side (reactants|products), examples+templates."""
    out = []
    for e in (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or []):
        for c in e.get(side) or []:
            s = _smiles_of(c, stereo)
            if s and "*" not in s:
                out.append(s)
    return out


def smiles_accuracy(pred, gt, stereo=False):
    """The KEY signal: are the reactant+product MOLECULES correct (RDKit canonical match)?

    SET-based (UNIQUE molecules) by design -- representation-robust. A conditions/ligand screen
    drawn compactly as 1 reaction_template + N inherit_template examples is NOT penalised (its
    unique molecule set still matches GT), while a genuinely UNDER-MATERIALISED scope grid still
    is (its product set is really smaller than GT's). Enumeration COUNT is scored separately by
    `completeness`, so it is deliberately decoupled here. The old count-sensitive multiset view is
    kept under `multiset` for diagnosis only. `by_role` splits reactants vs products."""
    from collections import Counter
    g_all, p_all = _all_mols(gt, stereo), _all_mols(pred, stereo)
    if not g_all:
        return None
    gs, ps = set(g_all), set(p_all)
    inter = len(gs & ps)
    rec = inter / len(gs)
    prec = inter / len(ps) if ps else 0.0
    f1 = 2 * rec * prec / (rec + prec) if (rec + prec) else 0.0
    by_role = {}
    for side in ("reactants", "products"):
        gr, pr = set(_role_mols(gt, side, stereo)), set(_role_mols(pred, side, stereo))
        if not gr:
            by_role[side] = None
            continue
        i = len(gr & pr)
        rr = i / len(gr)
        pp = i / len(pr) if pr else 0.0
        by_role[side] = {"recall": round(rr, 3), "precision": round(pp, 3),
                         "f1": round(2 * rr * pp / (rr + pp), 3) if (rr + pp) else 0.0,
                         "n_gt": len(gr), "n_pred": len(pr), "n_correct": i}
    gc, pc = Counter(g_all), Counter(p_all)
    mi = sum((gc & pc).values())
    multiset = {"recall": round(mi / len(g_all), 3),
                "precision": round(mi / len(p_all), 3) if p_all else 0.0,
                "n_gt": len(g_all), "n_pred": len(p_all), "n_correct": mi}
    return {"recall": round(rec, 3), "precision": round(prec, 3), "f1": round(f1, 3),
            "n_gt": len(gs), "n_pred": len(ps), "n_correct": inter,
            "by_role": by_role, "multiset": multiset}


def smiles_first_r(pred, gt, ns_weight=0.7):
    """SMILES-ONLY r(s): G * (ns_weight*F1_nostereo + (1-ns_weight)*F1_stereo), set-based.

    The 'just look at SMILES first' gate -- the molecule structures are the thing we actually care
    about; the other V2 fields are mostly already right, so the first training rounds optimise this
    alone. The validity floor G (0.10 if emit_full is invalid/empty/has leftover '*') is kept so a
    broken extraction cannot score high. Returns None if GT has no scorable molecules."""
    ns = smiles_accuracy(pred, gt, stereo=False)
    if ns is None:
        return None
    st = smiles_accuracy(pred, gt, stereo=True)
    G = 1.0 if gate_valid(pred) else 0.10
    f1_ns = ns["f1"]
    f1_st = st["f1"] if st else f1_ns
    blended = ns_weight * f1_ns + (1.0 - ns_weight) * f1_st
    return {"r": round(G * blended, 4), "G": G, "f1_nostereo": f1_ns, "f1_stereo": f1_st,
            "recall_nostereo": ns["recall"], "precision_nostereo": ns["precision"],
            "by_role": ns["by_role"]}


# ----------------------------------------------------------------------- completeness
def _completeness(pred, gt):
    ep, eg = _examples(pred), _examples(gt)
    count_recall = min(1.0, len(ep) / len(eg)) if eg else (1.0 if not ep else 0.0)
    if not ep:
        arity_ok = 0.0
    else:
        good = 0
        for e in ep:
            r_ok = any(_smiles_of(c) for c in (e.get("reactants") or []))
            p_ok = any(_smiles_of(c) for c in (e.get("products") or []))
            if r_ok and (p_ok or e.get("failed")):
                good += 1
        arity_ok = good / len(ep)
    return 0.5 * count_recall + 0.5 * arity_ok, {"count_recall": round(count_recall, 3),
                                                 "arity_ok": round(arity_ok, 3),
                                                 "n_pred_ex": len(ep), "n_gt_ex": len(eg)}


# ----------------------------------------------------------------------- conditions
def _cond_blocks(v2):
    """Return list of conditions-fields (each is the raw list-of-list) pooled from examples+templates."""
    blocks = []
    for e in _examples(v2):
        blocks.append(e.get("conditions") or [])
    for t in _templates(v2):
        blocks.append(t.get("conditions") or [])
    return blocks

_AMOUNT = re.compile(
    r"\(\s*[\d.]+\s*(?:mol\s*%|equiv|equivalents?|ml|µl|ul|mm|nm|m|g|mg|h\b|hr|min|wt\.?\s*%?|x|°?\s*c)\b[^)]*\)",
    re.I)
def _reagent_key(it):
    """Identity of a condition entry: canon SMILES if it has a structure, else amount-stripped text.
    This makes the template-vs-inline and 'L1' vs 'L1, 2,2'-bipyridine' variants comparable."""
    es = it.get("E-SMILES") or it.get("smiles")
    if es:
        s = norm_mol(es, False)
        if s and Chem is not None and Chem.MolFromSmiles(s) is not None:
            return "smi:" + s
    t = _AMOUNT.sub("", it.get("text") or "")
    return "txt:" + _norm_text(t)

def _cond_entries(v2):
    """Deduped (role, reagent-key) entries pooled from examples + TEMPLATES.
    Dedup is essential: scope grids carry conditions ONCE in the template, but some extractors
    repeat them inline in every example — without dedup the precision is destroyed."""
    ents = set()
    for blk in _cond_blocks(v2):
        for step in blk:
            items = step if isinstance(step, list) else [step]
            for it in items:
                if isinstance(it, dict):
                    ents.add((_norm_role(it.get("role")), _reagent_key(it)))
    return [(r, k) for r, k in ents if k not in ("txt:", "smi:")]

def _is_list_of_list(blk):
    return isinstance(blk, list) and (len(blk) == 0 or all(isinstance(s, list) for s in blk))

def _conditions(pred, gt):
    gt_ents = _cond_entries(gt)
    if not gt_ents:
        return None, {"applicable": False}  # GT has no conditions -> N/A
    pred_ents = _cond_entries(pred)

    # --- content: role-EQUIVALENCE-aware F1 via Hungarian matching (key = canon-SMILES or
    #     amount-stripped text). Role-equivalence (photocatalyst~catalyst, reductant~reagent, wildcard
    #     others/conditions) credits correct reagents that only differ in role label; Hungarian (vs the
    #     old greedy loop) avoids under-counting. See _role_compat / _max_match. ---
    def _key_match(gk, pk):
        if gk == pk:
            return True
        if gk.startswith("txt:") and pk.startswith("txt:"):
            a, b = gk[4:], pk[4:]
            return bool(a) and bool(b) and (a in b or b in a)
        return False
    def _compat(g, p):
        return _role_compat(g[0], p[0]) and _key_match(g[1], p[1])
    matched = _max_match(gt_ents, pred_ents, _compat)
    content = _f1(matched, len(pred_ents), len(gt_ents))

    # --- structure: every conditions field is list-of-list; multistep substep count ---
    ms = _is_multistep(gt)
    pred_blocks = [b for b in _cond_blocks(pred) if b]
    lol_ok = 1.0 if (not pred_blocks or all(_is_list_of_list(b) for b in pred_blocks)) else 0.0
    if ms:
        gt_steps = max((len(b) for b in _cond_blocks(gt) if b), default=1)
        pr_steps = max((len(b) for b in pred_blocks), default=0)
        step_ok = min(1.0, pr_steps / gt_steps) if gt_steps else 1.0
        structure = 0.5 * lol_ok + 0.5 * step_ok
    else:
        structure = lol_ok
    score = 0.4 * structure + 0.6 * content
    return score, {"applicable": True, "content_f1": round(content, 3),
                   "structure": round(structure, 3), "n_pred": len(pred_ents),
                   "n_gt": len(gt_ents), "multistep": ms}


# ----------------------------------------------------------------------- metrics (yield/ee/dr)
_METRIC_ROLES = {"yield", "ee", "dr", "er", "conversion", "selectivity"}
def _metric_entries(v2):
    """Handle both shapes: list [{role|type, of|for_product, text|content}] and dict {yield: '73%'}."""
    out = []
    for e in _examples(v2) + _templates(v2):
        ms = e.get("metrics")
        if isinstance(ms, dict):
            for k, val in ms.items():
                role = _norm_role(k)
                if role in _METRIC_ROLES:
                    out.append((role, _parse_num(str(val))))
        elif isinstance(ms, list):
            for m in ms:
                if isinstance(m, dict):
                    role = _norm_role(m.get("role") or m.get("type"))
                    if role in _METRIC_ROLES:
                        out.append((role, _parse_num(m.get("text") or m.get("content") or m.get("value") or "")))
    return [(r, v) for r, v in out if v is not None]

def _metrics(pred, gt):
    g = _metric_entries(gt)
    if not g:
        return None, {"applicable": False}
    p = _metric_entries(pred)
    used = [False] * len(p)
    matched = 0
    for (gr, gv) in g:
        for i, (pr, pv) in enumerate(p):
            if used[i] or pr != gr:
                continue
            tol = 2.0 if gr in ("yield", "ee", "er", "dr", "conversion", "selectivity") else 0.01
            if abs(pv - gv) <= tol:
                used[i] = True
                matched += 1
                break
    return _f1(matched, len(p), len(g)), {"applicable": True, "n_pred": len(p),
                                          "n_gt": len(g), "matched": matched}


# ----------------------------------------------------------------------- topology
def _id_to_smiles(v2, stereo=False):
    mp = {}
    for e in _examples(v2) + _templates(v2):
        for side in ("reactants", "products"):
            for c in e.get(side) or []:
                ident = c.get("identifier")
                s = _smiles_of(c, stereo)
                if ident and s:
                    mp.setdefault(str(ident), s)
    return mp

def _edge_sets(v2):
    mp = _id_to_smiles(v2)
    edges = []
    for ed in (v2.get("reaction_graph") or []):
        def resolve(key):
            vals = ed.get(key) or []
            if isinstance(vals, str):
                vals = [vals]
            return frozenset(mp.get(str(x), str(x)) for x in vals)
        edges.append((resolve("from_mols"), resolve("to_mols")))
    return edges

def _topology(pred, gt):
    flag_ok = 1.0 if _is_multistep(pred) == _is_multistep(gt) else 0.0
    if not _is_multistep(gt):
        # GT is single-step: the only topology signal is the flag. A scope grid that records one
        # edge per example is a valid representation choice, not an error -> do not penalise edges.
        return flag_ok, {"flag_ok": flag_ok, "edge_f1": None, "n_pred_edges": len(_edge_sets(pred)),
                         "n_gt_edges": 0, "note": "GT single-step; edges ignored"}
    ge, pe = _edge_sets(gt), _edge_sets(pred)
    if not ge:
        edge_f1 = 1.0 if not pe else max(0.0, 1.0 - 0.1 * len(pe))
    else:
        used = [False] * len(pe)
        matched = 0
        for (gf, gt_) in ge:
            gset = gf | gt_
            best, bj = -1, 0.0
            for i, (pf, pt) in enumerate(pe):
                if used[i]:
                    continue
                j = jaccard(pf | pt, gset)
                if j > bj:
                    bj, best = j, i
            if best >= 0 and bj >= 0.5:
                used[best] = True
                matched += 1
        edge_f1 = _f1(matched, len(pe), len(ge))
    return 0.3 * flag_ok + 0.7 * edge_f1, {"flag_ok": flag_ok, "edge_f1": round(edge_f1, 3),
                                           "n_pred_edges": len(pe), "n_gt_edges": len(ge)}


# ----------------------------------------------------------------------- identifiers + class
def _compound_ids(v2):
    """All COMPOUND-LABEL identifiers in a V2 — from identifier_map AND from each example/template
    compound's `identifier`. Models put labels per-compound even when they leave identifier_map empty;
    keying only on identifier_map (the old bug) wrongly scored 0 for correctly-labelled extractions."""
    ids = set()
    mp = v2.get("identifier_map") or {}
    if not isinstance(mp, dict):                 # models sometimes emit identifier_map as a string/list -> treat as empty, don't crash
        mp = {}
    for k, val in mp.items():
        for x in (k, val):
            x = str(x).strip()
            if x and len(x) <= 12 and not (Chem is not None and len(x) >= 8 and Chem.MolFromSmiles(x)):
                ids.add(x)
    for e in (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or []):
        for side in ("reactants", "products"):
            for c in e.get(side) or []:
                i = c.get("identifier")
                if i and str(i).strip():
                    ids.add(str(i).strip())
    return ids

def _identifiers(pred, gt):
    g = _compound_ids(gt)
    gcls = gt.get("reaction_class") or (gt.get("_meta") or {}).get("reaction_class")
    if not g and not gcls:
        return None, {"applicable": False}
    parts, w = [], []
    if g:
        p = _compound_ids(pred)
        parts.append(len(g & p) / len(g)); w.append(0.8)   # recall of GT compound labels (map OR per-compound)
    if gcls:
        pcls = pred.get("reaction_class") or (pred.get("_meta") or {}).get("reaction_class") or ""
        soft = 1.0 if _norm_text(pcls) == _norm_text(gcls) else (
            0.5 if pcls and (_norm_text(gcls) in _norm_text(pcls) or _norm_text(pcls) in _norm_text(gcls)) else 0.0)
        parts.append(soft); w.append(0.2)
    score = sum(p * wi for p, wi in zip(parts, w)) / sum(w)
    return score, {"applicable": True, "gt_ids": len(g), "class_in_gt": bool(gcls)}


# ----------------------------------------------------------------------- failed flags
def _failed_set(v2):
    out = set()
    for i, e in enumerate(_examples(v2)):
        has_p = any(_smiles_of(c) for c in (e.get("products") or []))
        failed = bool(e.get("failed")) or (not has_p and bool(e.get("reactants")))
        if failed:
            pid = next((str(c.get("identifier")) for c in (e.get("products") or []) if c.get("identifier")), None)
            if not pid:  # failed cells often have an empty product -> key by reactant ids
                rids = tuple(sorted(str(c.get("identifier")) for c in (e.get("reactants") or []) if c.get("identifier")))
                pid = "R:" + "|".join(rids) if rids else f"#{i}"
            out.add(pid)
    return out

def _failed(pred, gt):
    gf = _failed_set(gt)
    if not gf:
        return None, {"applicable": False, "note": "GT marks no failed examples"}  # don't penalise a convention GT doesn't use
    pf = _failed_set(pred)
    inter = len(gf & pf)
    return _f1(inter, len(pf), len(gf)), {"applicable": True, "gt_failed": sorted(gf),
                                          "pred_failed": sorted(pf)}


# ----------------------------------------------------------------------- main scorer
def _mol_set(comps, stereo):
    out = set()
    for c in (comps or []):
        s = _smiles_of(c, stereo)
        if s:
            out.add(s)
    return frozenset(out)

def _example_rxn(e, stereo=False):
    """One reaction as (reactants_smiles_set, products_smiles_set, condition_(bucket,key)_set)."""
    conds = set()
    for step in (e.get("conditions") or []):
        for it in (step if isinstance(step, list) else [step]):
            if isinstance(it, dict):
                k = _reagent_key(it)
                if k not in ("txt:", "smi:"):
                    conds.add((_role_bucket(it.get("role")), k))
    return _mol_set(e.get("reactants"), stereo), _mol_set(e.get("products"), stereo), frozenset(conds)

def reaction_f1(pred, gt, stereo=False):
    """Reaction-level SOFT vs HARD match F1 (RxnScribe/RxnIM convention, adapted to V2 examples for
    cross-paper comparability). SOFT = molecule set (reactants U products) equal, role-agnostic. HARD =
    reactants set == AND products set == AND every GT condition entry matched (role-equivalence). Reactions
    matched by Hungarian; P/R/F per figure (aggregate micro across figures in the re-score)."""
    G = [r for r in (_example_rxn(e, stereo) for e in _examples(gt)) if r[1]]
    P = [r for r in (_example_rxn(e, stereo) for e in _examples(pred)) if r[1]]
    if not G:
        return {"applicable": False}

    def soft_ok(g, p):
        return (g[0] | g[1]) == (p[0] | p[1])

    def _cond_pair(a, b):
        if a[1] == b[1]:
            ok_key = True
        elif a[1].startswith("txt:") and b[1].startswith("txt:"):
            x, y = a[1][4:], b[1][4:]
            ok_key = bool(x) and bool(y) and (x in y or y in x)
        else:
            ok_key = False
        return _role_compat(a[0], b[0]) and ok_key

    def hard_ok(g, p):
        if g[0] != p[0] or g[1] != p[1]:
            return False
        gc = list(g[2])
        if not gc:
            return True
        return _max_match(gc, list(p[2]), _cond_pair) == len(gc)

    soft = _max_match(G, P, soft_ok)
    hard = _max_match(G, P, hard_ok)
    return {"applicable": True, "n_gt": len(G), "n_pred": len(P),
            "soft_f1": round(_f1(soft, len(P), len(G)), 4), "soft_tp": soft,
            "hard_f1": round(_f1(hard, len(P), len(G)), 4), "hard_tp": hard}


def score_v2(pred: dict, gt: dict) -> dict:
    valid = gate_valid(pred)
    G = 1.0 if valid else GATE_FAIL

    structures, stereo, struct_dbg = _structures_and_stereo(pred, gt)
    completeness, comp_dbg = _completeness(pred, gt)
    conditions, cond_dbg = _conditions(pred, gt)
    topology, topo_dbg = _topology(pred, gt)
    metrics, met_dbg = _metrics(pred, gt)
    identifiers, id_dbg = _identifiers(pred, gt)
    failed, fail_dbg = _failed(pred, gt)

    dims = {
        "structures": structures, "completeness": completeness, "conditions": conditions,
        "topology": topology, "metrics": metrics, "stereo": stereo,
        "identifiers": identifiers, "failed": failed,
    }
    applicable = {k: v for k, v in dims.items() if v is not None}
    W = sum(WEIGHTS[k] for k in applicable)
    content = sum(WEIGHTS[k] * v for k, v in applicable.items()) / W if W else 0.0
    r = max(0.0, min(1.0, G * content))

    return {
        "r": round(r, 4),
        "gate": {"valid": valid, "G": G},
        "content": round(content, 4),
        "dims": {k: (round(v, 4) if v is not None else None) for k, v in dims.items()},
        # molecule-level SMILES accuracy (RDKit canonical SET match of EVERY reactant+product SMILES) —
        # the single most important signal; complements the reaction-level `structures` dimension.
        "smiles_accuracy": {"nostereo": smiles_accuracy(pred, gt, False),
                            "stereo": smiles_accuracy(pred, gt, True)},
        # reaction-level soft/hard match F1 (RxnScribe/RxnIM-style) for cross-benchmark comparability
        "reaction_f1": reaction_f1(pred, gt, False),
        # SMILES-only gate r(s): G * blend(F1_nostereo, F1_stereo). The 'just look at SMILES' score
        # for the first training rounds (push molecule correctness up before the other dimensions).
        "smiles_first": smiles_first_r(pred, gt),
        "weights_used": {k: WEIGHTS[k] for k in applicable},
        "debug": {"structures": struct_dbg, "completeness": comp_dbg, "conditions": cond_dbg,
                  "topology": topo_dbg, "metrics": met_dbg, "identifiers": id_dbg, "failed": fail_dbg},
    }


def _load(p):
    return json.loads(Path(p).read_text())


def main():
    ap = argparse.ArgumentParser(description="SkillOpt validation gate r(s) for V2 extraction")
    ap.add_argument("pred", nargs="?", help="predicted V2 json")
    ap.add_argument("gt", nargs="?", help="ground-truth V2 json (*.reviewed.json)")
    ap.add_argument("--paper", help="score every figure in a paper dir vs its *.reviewed.json")
    ap.add_argument("--pred-suffix", default="_output.v2_skill_extract.json",
                    help="prediction suffix replacing '.reviewed.json' in --paper mode")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    if a.paper:
        pdir = Path(a.paper)
        rows, rs = [], []
        for gtp in sorted(pdir.glob("*.reviewed.json")):
            if "diff" in gtp.name:
                continue
            predp = gtp.with_name(gtp.name.replace(".reviewed.json", a.pred_suffix))
            if not predp.exists():
                continue
            res = score_v2(_load(predp), _load(gtp))
            rs.append(res["r"]); rows.append((gtp.name.split("_")[0], res))
        if a.json:
            print(json.dumps({"figures": {n: r for n, r in rows},
                              "mean_r": round(sum(rs) / len(rs), 4) if rs else 0.0}, indent=1))
        else:
            print(f"{'fig':>4s} {'r':>6s} {'valid':>5s} {'struct':>6s} {'compl':>6s} "
                  f"{'cond':>6s} {'topo':>6s} {'metr':>6s} {'ster':>6s}")
            for n, res in rows:
                d = res["dims"]
                def g(k): return f"{d[k]:.3f}" if d[k] is not None else "  -  "
                print(f"{n:>4s} {res['r']:>6.3f} {str(res['gate']['valid'])[0]:>5s} "
                      f"{g('structures'):>6s} {g('completeness'):>6s} {g('conditions'):>6s} "
                      f"{g('topology'):>6s} {g('metrics'):>6s} {g('stereo'):>6s}")
            print("-" * 64)
            print(f"mean r(s) over {len(rs)} figs = {sum(rs)/len(rs):.4f}" if rs else "no figures scored")
        return

    if not (a.pred and a.gt):
        ap.error("give <pred> <gt>, or --paper <dir>")
    res = score_v2(_load(a.pred), _load(a.gt))
    print(json.dumps(res, indent=1, ensure_ascii=False) if a.json else _pretty(res))


def _pretty(res):
    lines = [f"r(s) = {res['r']}   (gate valid={res['gate']['valid']}, G={res['gate']['G']}, "
             f"content={res['content']})", "", "  dim           weight   score"]
    for k, w in WEIGHTS.items():
        v = res["dims"][k]
        used = k in res["weights_used"]
        lines.append(f"  {k:<13s} {w:>5.2f}   "
                     f"{(f'{v:.3f}' if v is not None else 'N/A (dropped)'):>14s}"
                     f"{'' if used else '   [renormalised out]'}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
