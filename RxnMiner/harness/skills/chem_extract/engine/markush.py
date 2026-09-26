"""engine.markush — the 反推 (reverse) + 带入 (fill) engine. READ THIS to understand
exactly what reverse/fill do; `tool.py` calls the canonical implementations in the parent
repo (`tools_product/uniparser_to_v2.py`, which wraps MarkushMapMulti + RDKit). The real
source of the two core functions is reproduced below verbatim so you can see the I/O — it
is NOT a paraphrase.

KEY POINT (do not be fooled by the name layer): `forward_fill` substitutes a value into a
markush template. The value may be EITHER a `*`-rooted fragment SMILES (`"*OC"`,
`"*c1ccccc1"`) — the direct, fully-general path — OR a substituent NAME (`"OMe"`, `"Ph"`)
which a convenience layer resolves to a fragment. The name layer is sugar; the SMILES path
always works and is never limited to a lookup table.

`reverse_r(template, concrete)` ONLY works when `template` is a substructure of `concrete`
(same scaffold). Reverse the PRODUCT scaffold against a CONCRETE PRODUCT. Validate by
round-trip: forward_fill(template, reverse_r(template, x)) == canon(x).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = os.environ.get("CHEM_REPO")
if not _REPO:
    for p in Path(__file__).resolve().parents:
        if (p / "tools_product/uniparser_to_v2.py").exists():
            _REPO = str(p); break
sys.path.insert(0, _REPO)
sys.path.insert(0, str(Path(_REPO) / "tools_product"))

# the canonical, battle-tested implementations (what tool.py actually runs):
from uniparser_to_v2 import (  # noqa: E402,F401
    reverse_r as _reverse_r_mm,  # 反推 (MarkushMapMulti) -> {} when MarkushMapMulti is unavailable (_HAVE_MM False)
    forward_fill,     # 带入:  (markush_template, {label: "*frag" | "name"}) -> concrete SMILES | None
    canon,            # RDKit canonical SMILES (strips <sep>/<a> tags)
    is_markush,       # does an E-SMILES carry an R placeholder
    strip_esmiles,    # E-SMILES -> bare SMILES body
)


def _rdkit_reverse_r(template, concrete):
    """Pure-RDKit `reverse` fallback for when MarkushMapMulti is unavailable (the MM path returns {} and
    markush_enumeration can never populate). Extracts the R group(s) at the template's '*'/[*:n] position(s)
    via RDKit ReplaceCore: strip the template's dummy atoms to get the bare scaffold, match it in the concrete,
    and read out the leftover sidechains as {label: '*frag'} (same shape as the MarkushMap path). Mirrors the
    RDKit fallback `fill` already has (fill_star_rdkit)."""
    import re
    try:
        from rdkit import Chem
        from rdkit import RDLogger; RDLogger.DisableLog("rdApp.*")
    except Exception:
        return {}
    t = Chem.MolFromSmiles(strip_esmiles(template) or "")
    m = Chem.MolFromSmiles(strip_esmiles(concrete) or "")
    if t is None or m is None:
        return {}
    rw = Chem.RWMol(t)                                          # scaffold = template minus its dummy atoms
    for idx in sorted([a.GetIdx() for a in t.GetAtoms() if a.GetAtomicNum() == 0], reverse=True):
        rw.RemoveAtom(idx)
    core = rw.GetMol()
    try:
        Chem.SanitizeMol(core)
    except Exception:
        pass
    if core.GetNumAtoms() == 0:
        return {}
    rest = Chem.ReplaceCore(m, core, labelByIndex=True)
    if rest is None:
        return {}
    out = {}
    frags = Chem.GetMolFrags(rest, asMols=True, sanitizeFrags=False)
    multi = len(frags) > 1
    for i, fr in enumerate(frags):
        try:
            smi = re.sub(r"\[\d*\*\]", "*", Chem.MolToSmiles(fr))
        except Exception:
            continue
        if "*" not in smi:
            smi = "*" + smi
        out["R" + (str(i + 1) if multi else "")] = smi
    return out


def reverse_multi(template_bare, concrete_bare):
    """POSITIONAL multi-'*' reverse via RDKit RGroupDecomposition. Returns [frag-per-'*' in template order]
    (each '*frag', None for an unsubstituted/H position) or None if the scaffold doesn't match the concrete.
    Fixes the case the dict-keyed reverse collapses: a template with several bare '*' that need DIFFERENT
    substituents (e.g. '*/C=C/C(*)=O' -> R1=alkyl, R2=H). A scaffold mismatch (wrong ring size / fused ring =
    an upstream OCSR/template error) correctly returns None so the enumerate roundtrip flags the cell 'failed'.

    Stereo-strip retry: RGD will not match a scaffold that carries '@' or '/\\' against a target whose stereo
    annotation differs (common: the markush template draws stereo the concrete drops, or vice versa). When the
    as-drawn scaffold fails to match, retry with the scaffold's tetrahedral + double-bond stereo stripped (the
    R fragments still keep their OWN stereo, read off the concrete). This is the same fix as agent/rdkit_reverse
    and unblocks substituent-scope grids whose product scaffold is stereodefined (e.g. pyrrolidinone rings)."""
    for _scaf in (template_bare, str(template_bare or "").replace("@", "").replace("/", "").replace("\\", "")):
        _r = _reverse_multi_once(_scaf, concrete_bare)
        if _r is not None:
            return _r
    return None


def _reverse_multi_once(template_bare, concrete_bare):
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem import rdRGroupDecomposition as _rgd
        RDLogger.DisableLog("rdApp.*")
        import re as _re
    except Exception:
        return None
    t = Chem.MolFromSmiles(template_bare or ""); m = Chem.MolFromSmiles(concrete_bare or "")
    if t is None or m is None:
        return None
    core = Chem.RWMol(t); n = 0
    for a in core.GetAtoms():
        if a.GetAtomicNum() == 0:
            n += 1; a.SetAtomMapNum(n)
    if n == 0:
        return None
    core = core.GetMol()
    try:
        Chem.SanitizeMol(core)
    except Exception:
        pass
    try:
        ps = _rgd.RGroupDecompositionParameters(); ps.onlyMatchAtRGroups = True; ps.removeHydrogensPostMatch = True
        res, _f = _rgd.RGroupDecompose([core], [m], asSmiles=True, options=ps)
    except Exception:
        return None
    if not res:
        return None
    frags = [None] * n
    for k, v in res[0].items():
        if not k.startswith("R"):
            continue
        idx = int(_re.sub(r"\D", "", k) or 0)
        if 1 <= idx <= n:
            f = _re.sub(r"\[\d*\*:?\d*\]", "*", v)
            frags[idx - 1] = f if "*" in f else ("*" + f)
    return frags


def fill_multi(template_bare, frags):
    """POSITIONAL fill: substitute the i-th '*' of the template with frags[i] (each '*frag'; None -> H).
    Inverse of reverse_multi; the enumerate grid validates it by roundtrip before trusting a cell."""
    try:
        from rdkit import Chem, RDLogger
        RDLogger.DisableLog("rdApp.*")
    except Exception:
        return None
    t = Chem.MolFromSmiles(template_bare or "")
    if t is None:
        return None
    core = Chem.RWMol(t); k = 0
    for a in core.GetAtoms():
        if a.GetAtomicNum() == 0:
            k += 1; a.SetAtomMapNum(k)
    combined = core.GetMol()
    for i, fr in enumerate(frags, start=1):
        fm = Chem.MolFromSmiles(fr if fr else "*[H]")
        if fm is None:
            return None
        rw = Chem.RWMol(fm)
        for a in rw.GetAtoms():
            if a.GetAtomicNum() == 0:
                a.SetAtomMapNum(i)
        combined = Chem.CombineMols(combined, rw.GetMol())
    try:
        return Chem.MolToSmiles(Chem.molzip(combined))
    except Exception:
        return None


# ===== OPTIONAL upstream molecular assembly integration (env FHR_REVERSE=1; default OFF) =====
import re as _re_fhr
_FHR_ON = os.environ.get("FHR_REVERSE") == "1"
_fhr_step0 = None
_fhr_assemble = None
if _FHR_ON:
    try:
        _MA = os.path.join(_REPO, "third_party/molecular_assembly")
        if _MA not in sys.path:
            sys.path.insert(0, _MA)
        from ma_paths import setup_src_path as _sp_fhr
        _sp_fhr()
        from decompose import step0_scaffold_map as _fhr_step0
        from assembly import assemble_fragments as _fhr_assemble
    except Exception:
        _fhr_step0 = None
        _fhr_assemble = None

def _labels_of(es):
    # keep FULL label (e.g. R[1]); stripping [N] made keep={R} never match r_dict keys R[1]
    return [lb.strip()
            for _i, lb in _re_fhr.findall(r"<a>(\d+):([^<]+)</a>", es or "")]

def _fhr_peel_dict(cand):
    d = {}
    for sub in getattr(cand, "peeled_substituents", []) or []:
        lab = (sub.get("slot_ids") or [sub.get("display_label")])[0]
        if not lab:
            continue
        f = (sub.get("smiles") or "").split("<sep>")[0].strip()
        f = _re_fhr.sub(r"\[\d+\*\]", "*", f).replace("[*]", "*")
        if f:
            d[lab] = f
    return d

def _fhr_reverse_r(template_product_esmiles, concrete_product_smiles):
    """fhr peel -> {label: '*frag'}; candidate picked by (label coverage, roundtrip_ok, peel_topology_ok)."""
    if _fhr_step0 is None:
        return None
    try:
        res = _fhr_step0(concrete_product_smiles, template_product_esmiles, render_images=False)
    except Exception:
        return None
    cands = getattr(res, "matches", None) or []
    if not cands:
        return None
    want = set(_labels_of(template_product_esmiles))
    def _cov(c):
        return len(want & set(_fhr_peel_dict(c).keys()))
    cands = sorted(cands, key=lambda c: (-_cov(c), not getattr(c, "roundtrip_ok", False),
                                         not getattr(c, "peel_topology_ok", False)))
    return _fhr_peel_dict(cands[0]) or None

# capture the imported (baseline) fill before shadowing it
_our_forward_fill = forward_fill

def forward_fill(template_esmiles, r_dict):  # noqa: F811 -- fhr-assemble-first override
    """带入: fhr assemble_fragments first (its own assembler handles its R representation), else our molzip."""
    if not template_esmiles:
        return None
    if not is_markush(template_esmiles):
        return canon(template_esmiles)                      # constant/non-markush passthrough (unchanged)
    if _FHR_ON and _fhr_assemble is not None:
        try:
            labels = _labels_of(template_esmiles)                 # full template labels, e.g. R[1]
            _n = lambda x: _re_fhr.sub(r"[\[\]]", "", str(x))     # R[1] <-> R1 tolerant match
            rmap = {_n(k): v for k, v in dict(r_dict).items()}     # baseline emits R1, fhr peel emits R[1]
            lines = [template_esmiles] + [
                "{0}<sep><a>0:{1}</a>".format(rmap[_n(lab)], lab)  # <a> label = TEMPLATE label (fhr pairs on it)
                for lab in labels
                if _n(lab) in rmap and isinstance(rmap[_n(lab)], str) and rmap[_n(lab)].startswith("*")
            ]
            if len(lines) > 1:
                res = _fhr_assemble(lines)
                smi = getattr(res, "smiles", "") or ""
                if getattr(res, "success", False) and smi and "*" not in smi:
                    c = canon(smi, stereo=True)   # keep E/Z + tetrahedral fhr preserved
                    if c:
                        return c
        except Exception:
            pass
    return _our_forward_fill(template_esmiles, r_dict)       # molzip/graph_substitute fallback

def reverse_r(template_product_esmiles, concrete_product_smiles):  # noqa: F811 -- wrap with an RDKit fallback
    """反推: our baseline (MarkushMapMulti -> RDKit); when FHR_REVERSE=1, fhr step0 peel overrides per-label and
    our baseline fills any label fhr under-peels (union), so a missing NO2/etc. no longer breaks the fill."""
    try:
        base = _reverse_r_mm(template_product_esmiles, concrete_product_smiles)
    except Exception:
        base = None
    if not base:
        base = _rdkit_reverse_r(template_product_esmiles, concrete_product_smiles)
    base = base or {}
    if not _FHR_ON:
        return base
    fhr = _fhr_reverse_r(template_product_esmiles, concrete_product_smiles)
    if not fhr:
        return base
    merged = dict(base)      # ours covers labels fhr under-peels
    merged.update(fhr)       # fhr (more accurate per-label) overrides where present
    return merged

# ===== HIGH-LEVEL fhr reverse_assemble (the CORRECT reactant-derivation entry) =====
# The enumerate path (reverse_r + forward_fill above) uses fhr's LOW-level step0_scaffold_map +
# assemble_fragments. That under-peels and returns None for organometallic reactants (e.g. an
# alkylzinc `*[Zn][Br]`). fhr's OWN high-level `reverse_assemble(target, product_tpl, [reactant_tpls])`
# peels the product R-groups AND fills every reactant template in one call, and derives those correctly
# (cell 39 of 03e9c566 -> `Br[Zn]C1CCC1`). Prefer the newer v2 copy (better aryl ring-substituent
# handling). This is loaded lazily + independently of the FHR_REVERSE low-level path.
_RA_FN = None
_RA_LOADED = False

def _load_reverse_assemble():
    """Lazily import fhr's high-level reverse_assemble callable (prefer molecular_assembly_v2, fall
    back to the old vendored flat copy). Cached. Returns the callable or None if neither loads."""
    global _RA_FN, _RA_LOADED
    if _RA_LOADED:
        return _RA_FN
    _RA_LOADED = True
    # (1) prefer molecular_assembly_v2 (package `chemistry`, flat `esmiles`; better ring R handling)
    v2 = os.path.join(_REPO, "third_party/molecular_assembly_v2")
    if os.path.isdir(os.path.join(v2, "src", "chemistry")):
        try:
            for p in (os.path.join(v2, "src"), v2):
                if p in sys.path:
                    sys.path.remove(p)
                sys.path.insert(0, p)
            # v2's `chemistry`/`esmiles` differ from the OLD flat vendored copy; drop any stale cached
            # copies so they re-import from v2. The OLD low-level bindings (_fhr_step0/_fhr_assemble) are
            # already captured as function objects and are unaffected by clearing these module entries.
            for _m in [k for k in list(sys.modules)
                       if k == "esmiles" or k == "chemistry" or k.startswith("chemistry.")]:
                _mf = getattr(sys.modules.get(_m), "__file__", "") or ""
                if v2 not in _mf:
                    sys.modules.pop(_m, None)
            from chemistry import reverse_assemble as _ra   # noqa: F401
            _RA_FN = _ra
            return _RA_FN
        except Exception:
            _RA_FN = None
    # (2) fallback: OLD vendored flat copy (src/reverse_assembly.py)
    try:
        old_src = os.path.join(_REPO, "third_party/molecular_assembly", "src")
        if os.path.isfile(os.path.join(old_src, "reverse_assembly.py")):
            if old_src not in sys.path:
                sys.path.insert(0, old_src)
            import reverse_assembly as _ra_old   # noqa: F401
            _RA_FN = getattr(_ra_old, "reverse_assemble", None)
    except Exception:
        _RA_FN = None
    return _RA_FN


def reverse_assemble_reactants(target_product, product_template, reactant_templates, *, match_index=None):
    """反应物派生(高层):给一个具体产物 + 其 markush 产物模板(带 <sep> 标签的 E-SMILES)+ 各反应物
    markush 模板,一次调用推出每个反应物的具体 SMILES(peel 产物 R -> 逐条填反应物模板)。这是 fhr 的
    正确入口 `chemistry.reverse_assemble`,不是 enumerate 用的低层 step0+assemble(会 under-peel、对
    烷基锌等有机金属返回 None)。

    Returns a list aligned with `reactant_templates`:
        [{template, smiles, success, partial, missing_labels}, ...]
    or None (fhr unavailable / product could not be peeled)."""
    if not (target_product and product_template and reactant_templates):
        return None
    ra = _load_reverse_assemble()
    if ra is None:
        return None
    try:
        kw = {"render_images": False}
        if match_index is not None:
            kw["match_index"] = int(match_index)
        res = ra(str(target_product), str(product_template),
                 [str(t) for t in reactant_templates], **kw)
    except Exception:
        return None
    rows = (getattr(res, "reactants", None) or []) if res is not None else []
    if not rows:
        return None
    out = []
    for row in rows:
        smi = (getattr(row, "assembled_smiles", "") or "").split("<sep>")[0].strip()
        out.append({
            "template": getattr(row, "template_line", "") or "",
            "smiles": smi or None,
            "success": bool(getattr(row, "success", False)),
            "partial": bool(getattr(row, "partial", False)),
            "missing_labels": list(getattr(row, "missing_labels", []) or []),
        })
    return out

# ---------------------------------------------------------------------------
# VERBATIM REFERENCE COPY of the two core functions (source of truth above).
# Engines under the hood: MarkushMapMulti `RMappingResult.from_mapping` (reverse) and
# RDKit `graph_substitute` (fill, in repo agent/markush_substitute.py), with a MarkushMap
# dict-inject fallback. Reproduced so you can read the exact behaviour:
# ---------------------------------------------------------------------------
_REFERENCE = r'''
def reverse_r(template_product_esmiles, concrete_product_smiles):
    """反推: derive {label: '*frag'} from (Markush template product, concrete product)."""
    if not _HAVE_MM:
        return {}
    cp = strip_esmiles(concrete_product_smiles)
    out = {}
    try:
        res = RMappingResult.from_mapping(template_product_esmiles, cp)   # MarkushMapMulti
    except Exception:
        return {}
    for g in getattr(res, "r_groups", []) or []:
        conns = getattr(g, "connections", None) or []
        if not conns:
            continue
        c = conns[0]
        frag = reverse_to_star(g.smiles, c.r_group_attach_point)   # add '*' at the attach atom
        if frag:
            out[_clean_label(c.label)] = frag                      # 'R[1]'/'R1'/'R' -> 'R'
    return out

def forward_fill(template_esmiles, r_dict):
    """带入: fill a Markush template with {label: '*frag' OR 'name'} -> concrete SMILES."""
    if not template_esmiles:
        return None
    if not is_markush(template_esmiles):
        return canon(template_esmiles)
    try:
        filled = graph_substitute(template_esmiles, dict(r_dict))   # RDKit graph-substitute
        out = filled[0] if isinstance(filled, tuple) else filled
        if isinstance(out, str) and "*" not in out:
            c = canon(out)
            if c:
                return c
    except Exception:
        pass
    return _mm_inject_fill(template_esmiles, r_dict)                # MarkushMap inject fallback
'''
