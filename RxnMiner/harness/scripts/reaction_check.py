"""GT-FREE reaction self-consistency check: every scope-grid example must CONFORM TO ITS TEMPLATE (通式).

The reaction_template defines the generic transformation (reactant scaffolds + product scaffold, with '*' at
the varying positions). A concrete example is valid only if its product contains the template PRODUCT scaffold
and its reactants contain the template REACTANT scaffolds -- the '*' positions may be anything, but the fixed
core must be present. A cell that violates this is an OCSR / model-assembly error we can flag WITHOUT any GT,
so it works live (the agent can self-check before emit) and localizes the error to the product or reactant side.

Robust to multi-'*' templates (where markush reverse_r is weak): we treat the template scaffold as an RDKit
SMARTS query with '*' = wildcard atom and test substructure containment -- no R-group inversion needed.

Returns per example: {id, product_conforms, reactants_conform, status, detail}. status:
  ok                  -- product + reactants all contain their template scaffold
  product_off_template-- the product does NOT contain the template product core (likely product OCSR/model error)
  reactant_off_template-- a reactant does NOT contain its template reactant core (reactant OCSR/derivation error)
  unverifiable        -- couldn't build a query (template/example unparseable)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
try:
    from rdkit import Chem, RDLogger
    RDLogger.DisableLog("rdApp.*")
except Exception:
    Chem = None


def _bare(s):
    return (s or "").split("<sep>")[0].strip()


def _scaffold_query(smi):
    """Template scaffold -> SMARTS query where '*' (dummy) matches ANY atom. Stereo stripped (connectivity check)."""
    if Chem is None:
        return None
    m = Chem.MolFromSmiles(_bare(smi))
    if m is None:
        return None
    Chem.RemoveStereochemistry(m)
    return Chem.MolFromSmarts(Chem.MolToSmiles(m).replace("[*]", "*"))


def _contains(example_smi, query):
    """Does the concrete example structure contain the template scaffold? None if unparseable."""
    if Chem is None or query is None:
        return None
    m = Chem.MolFromSmiles(_bare(example_smi))
    if m is None:
        return None
    Chem.RemoveStereochemistry(m)
    try:
        return m.HasSubstructMatch(query)
    except Exception:
        return None


def _smis(group, side):
    return [(_bare(c.get("E-SMILES") or c.get("smiles"))) for c in (group.get(side) or [])
            if isinstance(c, dict) and _bare(c.get("E-SMILES") or c.get("smiles"))]


def _variable_frags(example_smi, query):
    """The example atoms OUTSIDE the template scaffold = the per-cell VARIABLE substituent(s). Returns their
    fragment SMILES (>=4 heavy atoms, to ignore a lone methyl/halogen). None if unmatched/unparseable."""
    if Chem is None or query is None:
        return None
    m = Chem.MolFromSmiles(_bare(example_smi))
    if m is None:
        return None
    Chem.RemoveStereochemistry(m)
    match = m.GetSubstructMatch(query)
    if not match:
        return None
    var = set(range(m.GetNumAtoms())) - set(match)
    if not var:
        return []
    bonds = [b.GetIdx() for b in m.GetBonds() if b.GetBeginAtomIdx() in var and b.GetEndAtomIdx() in var]
    if not bonds:
        return []
    try:
        fs = Chem.MolToSmiles(Chem.PathToSubmol(m, bonds))
    except Exception:
        return []
    return [x for x in fs.split(".") if Chem.MolFromSmiles(x) and Chem.MolFromSmiles(x).GetNumHeavyAtoms() >= 4]


def _frag_in_any(frag, smis):
    """Does the variable fragment appear as a substructure in any of `smis` (e.g. the reactants)?"""
    if Chem is None:
        return None
    q = Chem.MolFromSmiles(frag)
    if q is None:
        return None
    for s in smis:
        m = Chem.MolFromSmiles(_bare(s))
        if m is not None:
            Chem.RemoveStereochemistry(m)
            if m.HasSubstructMatch(q):
                return True
    return False


def _is_markush(s):
    return "*" in (s or "")


def reaction_check(v2):
    templates = v2.get("reaction_templates") or []
    examples = v2.get("selected_examples") or []
    if not templates or not examples:
        return {"applicable": False, "reason": "needs >=1 template and >=1 example"}
    single = len(templates) == 1

    def template_for(ex, ei):
        if single:
            return templates[0]
        inh = ex.get("inherit_template")
        if isinstance(inh, int) and 0 <= inh < len(templates):
            return templates[inh]
        # string id match, else positional, else first
        for t in templates:
            if inh and inh in (t.get("template_id"), t.get("id"), t.get("label")):
                return t
        return templates[0]

    rows, n_ok = [], 0
    prod_bad = react_bad = unver = 0
    for ei, ex in enumerate(examples):
        if not isinstance(ex, dict):
            continue
        t = template_for(ex, ei)
        pid = next((str(p.get("identifier")).strip() for p in (ex.get("products") or [])
                    if isinstance(p, dict) and str(p.get("identifier") or "").strip()), "")
        rid = pid or str(ex.get("instance_id") or ei)

        # PRODUCT: example product(s) must each contain SOME template product scaffold
        tq = [q for q in (_scaffold_query(s) for s in _smis(t, "products")) if q is not None]
        eps = _smis(ex, "products")
        prod_conf = None
        if tq and eps:
            checks = [any(_contains(ep, q) for q in tq) for ep in eps]
            prod_conf = all(c is True for c in checks) if all(c is not None for c in checks) else None

        # REACTANTS: each MARKUSH template reactant core must appear in some example reactant; a CONSTANT template
        # reactant must be present. (We don't penalize EXTRA reactants here -- that's a precision concern.)
        ers = _smis(ex, "reactants")
        trs = _smis(t, "reactants")
        react_conf, missing = None, []
        if trs and ers:
            react_conf = True
            for tr in trs:
                if _is_markush(tr):
                    q = _scaffold_query(tr)
                    hit = any(_contains(er, q) for er in ers) if q is not None else None
                else:
                    tc = Chem.MolToSmiles(Chem.MolFromSmiles(_bare(tr))) if (Chem and Chem.MolFromSmiles(_bare(tr))) else None
                    hit = any(Chem.MolFromSmiles(_bare(er)) is not None and
                              Chem.MolToSmiles(Chem.MolFromSmiles(_bare(er))) == tc for er in ers) if tc else None
                if hit is False:
                    react_conf = False; missing.append(tr)
                elif hit is None:
                    react_conf = react_conf if react_conf is False else None

        # L2 R-CONSISTENCY: a reaction carries its variable group from reactant to product, so every variable
        # fragment in the product must appear in some reactant. A product fragment present in NO reactant means
        # the two sides DISAGREE on the substituent (one was misread) -- a GT-free inconsistency.
        r_inconsistent = []
        if prod_conf and tq and ers:
            for ep in eps:
                for q in tq:
                    frags = _variable_frags(ep, q)
                    if frags:
                        for fr in frags:
                            if _frag_in_any(fr, ers) is False:
                                r_inconsistent.append(fr)
                        break

        if prod_conf is False:
            status = "product_off_template"; prod_bad += 1
        elif react_conf is False:
            status = "reactant_off_template"; react_bad += 1
        elif r_inconsistent:
            status = "R_inconsistent"; react_bad += 1
        elif prod_conf is None and react_conf is None:
            status = "unverifiable"; unver += 1
        else:
            status = "ok"; n_ok += 1
        rows.append({"id": rid, "product_conforms": prod_conf, "reactants_conform": react_conf,
                     "status": status, "off_template_reactants": missing,
                     "inconsistent_fragments": r_inconsistent})

    n = len(rows)
    return {"applicable": True, "n": n, "n_ok": n_ok,
            "n_product_off": prod_bad, "n_reactant_off": react_bad, "n_unverifiable": unver,
            "conform_rate": round(n_ok / n, 3) if n else None,
            "flagged": [r for r in rows if r["status"] not in ("ok", "unverifiable")],
            "rows": rows}
