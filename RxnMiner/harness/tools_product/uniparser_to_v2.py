#!/usr/bin/env python3
"""uniparser_to_v2 — adapter from a UniParser per-figure result to V2 schema.

P1 of the product roadmap (runbook/18 §9.4). Pure transform: takes ONE UniParser
`get_result(objects=True)` raw JSON (a single figure / snip) + light figure metadata,
emits a V2 paper-figure dict whose shape matches `agent/state_ops.finalize` and is
scored by `tools_product/score_paper.py`.

Two laws (runbook/18 §1): structure is verbatim from UniParser perception (we never
hand-type SMILES); the only computation is the deterministic R-group engine
(反推 MarkushMap from_mapping → 带入 RDKit graph_substitute).

UniParser real output shape (verified): result["objects"] = flat list of
  {class, confidence, float_xyxy, page, str}
where `str` is a LaTeX-wrapped payload:
  molecule   -> \\begin{molecule}\\n<E-SMILES>\\n\\end{molecule}
  expression -> \\begin{reaction}\\n[{'prev_mols':[..],'post_mols':[..],'condition':{..}}]\\n\\end{reaction}
  moleculeid -> \\begin{moleculeid}\\n78%o yield\\n\\end{moleculeid}

Pipeline per figure:
  1. parse objects -> molecules / expressions / yields (in document order)
  2. expressions -> reaction_templates (Markush prev_mols -> post_mols)
  3. concrete molecule immediately followed by a yield -> an example PRODUCT
  4. per example product: 反推 R from (template_product, concrete_product)
     -> 带入 R into the template reactants -> concrete reactants -> selected_example
  5. fallbacks: concrete reaction in the expression -> emit directly;
     反推 failure -> product-only example (recall salvage, flagged in _source).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import signal
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "_external/MarkushMapMulti/graph_match"))

from rdkit import Chem  # noqa: E402
from rdkit import RDLogger  # noqa: E402

RDLogger.DisableLog("rdApp.*")

# --- engine imports (validated in P1) -----------------------------------------
from agent.markush_substitute import graph_substitute  # 带入 (RDKit, *-aware)  # noqa: E402

try:
    from mapping_result import RMappingResult  # 反推 (MarkushMap from_mapping)
    _HAVE_MM = True
except Exception:  # pragma: no cover
    _HAVE_MM = False

# --- canon (mirror the scorer so "valid" == "scoreable") ----------------------
_TAG_A = re.compile(r"<a>[^<]*</a>")
_TAG_R = re.compile(r"<r>[^<]*</r>")


def strip_esmiles(esmiles: str) -> str:
    """E-SMILES -> bare SMILES body: drop <a>/<r> tags, <sep>->'.', <dum>->'*'."""
    if not esmiles:
        return ""
    s = _TAG_A.sub("", esmiles)
    s = _TAG_R.sub("", s)
    s = s.replace("<sep>", ".").replace("<dum>", "*")
    return s.strip(". ").strip()


def canon(esmiles: Optional[str], stereo: bool = False) -> Optional[str]:
    if not esmiles or not isinstance(esmiles, str):
        return None
    s = strip_esmiles(esmiles)
    if not s:
        return None
    m = Chem.MolFromSmiles(s)
    if m is None:
        return None
    return Chem.MolToSmiles(m, isomericSmiles=stereo)


def is_markush(esmiles: str) -> bool:
    return bool(esmiles) and ("*" in esmiles or "<a>" in esmiles or "<r>" in esmiles)


# --- 反推 fragment normalizer (THE valence fix, validated paper47 0/8 -> 5/8) --
def reverse_to_star(rg_smiles: str, attach_mapnum: int) -> Optional[str]:
    """MarkushMap from_mapping returns r_group.smiles with the attaching atom
    carrying atom-map :attach_mapnum. Turn it into a `*`-anchored fragment.
    The attachment consumes one H on that atom -> clear an explicit H before
    adding the dummy, else valence overflow -> sanitize failure -> None."""
    m = Chem.MolFromSmiles(rg_smiles)
    if m is None:
        return None
    tgt = None
    for a in m.GetAtoms():
        if a.GetAtomMapNum() == attach_mapnum:
            tgt = a.GetIdx()
    for a in m.GetAtoms():
        a.SetAtomMapNum(0)
    if tgt is None:
        try:
            return Chem.MolToSmiles(m)
        except Exception:
            return None
    rw = Chem.RWMol(m)
    ta = rw.GetAtomWithIdx(tgt)
    ta.SetNumExplicitHs(max(0, ta.GetNumExplicitHs() - 1))
    ta.SetNoImplicit(False)
    d = rw.AddAtom(Chem.Atom(0))
    rw.AddBond(tgt, d, Chem.BondType.SINGLE)
    mol = rw.GetMol()
    try:
        Chem.SanitizeMol(mol)
        return Chem.MolToSmiles(mol)
    except Exception:
        return None


# --- MM safety guards: MarkushMapMulti.from_mapping runs an unbounded-recursion MCS clique
# search (no size/time bound) that STACK-OVERFLOWS or RDKit-C-segfaults on dense fused/bridged
# cages, killing the whole process. Guard with (1) a heavy-atom size gate that skips the
# pathological inputs BEFORE calling MM (prevents the segfault), and (2) a SIGALRM timeout that
# aborts a hung MCS recursion (main-thread only; production tool.py runs MM as a CLI subprocess =
# main thread). Either trip -> return {} so the caller falls back to the pure-RDKit reverse. -----
_MM_TIMEOUT_S = int(os.environ.get("MM_TIMEOUT_S", "20"))
_MM_MAX_HEAVY = int(os.environ.get("MM_MAX_HEAVY", "80"))


class _MMTimeout(Exception):
    pass


def _heavy_atoms(smi: str) -> int:
    m = Chem.MolFromSmiles(smi or "")
    return m.GetNumHeavyAtoms() if m is not None else 0


def reverse_r(template_product_esmiles: str, concrete_product_smiles: str) -> Dict[str, str]:
    """反推: derive {label: '*frag'} from (Markush template product, concrete product).
    Size/timeout guarded -- returns {} on oversized or hung inputs (caller falls to RDKit reverse)."""
    # NEW PIPELINE (USE_MOLPARSERUTILS=1): RDKit RGroupDecomposition reverse — substructure-based, ~0.00s,
    # round-trip self-checked. Solved the hardest cases 10/10 where MarkushMap MCS is 0/10 (NP-hard, 358s).
    # Falls through to the MarkushMap path below on empty (so behavior is a strict superset).
    try:
        from agent.rdkit_reverse import ENABLED as _RDK_REV, rdkit_reverse as _rdkit_reverse
        if _RDK_REV:
            _r = _rdkit_reverse(template_product_esmiles, concrete_product_smiles)
            if _r:
                return _r
    except Exception:
        pass
    if not _HAVE_MM:
        return {}
    cp = strip_esmiles(concrete_product_smiles)
    tp = strip_esmiles(template_product_esmiles)
    if _heavy_atoms(cp) > _MM_MAX_HEAVY or _heavy_atoms(tp) > _MM_MAX_HEAVY:
        return {}                                          # SIZE GATE: skip pathological cages (segfault source)
    out: Dict[str, str] = {}
    _use_alarm = threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM")
    _old = None
    if _use_alarm:
        def _on_alarm(signum, frame):
            raise _MMTimeout()
        _old = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(_MM_TIMEOUT_S)
    try:
        res = RMappingResult.from_mapping(template_product_esmiles, cp)
    except Exception:                                      # incl. _MMTimeout (hung MCS recursion)
        return {}
    finally:
        if _use_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, _old)
    for g in getattr(res, "r_groups", []) or []:
        conns = getattr(g, "connections", None) or []
        if not conns:
            continue
        c = conns[0]
        frag = reverse_to_star(g.smiles, c.r_group_attach_point)
        if frag:
            out[_clean_label(c.label)] = frag
    return out


def _clean_label(label: str) -> str:
    """Normalise MarkushMap labels: 'R[1]' / 'R1' / 'R' -> the bare token used in <a>."""
    if label is None:
        return "R"
    return label.replace("[", "").replace("]", "").strip() or "R"


# --- 带入: graph_substitute (RDKit) primary, MM-inject fallback ---------------
def forward_fill(template_esmiles: str, r_dict: Dict[str, str]) -> Optional[str]:
    """带入: fill a Markush template E-SMILES with {label: '*frag'} -> concrete SMILES."""
    if not template_esmiles:
        return None
    if not is_markush(template_esmiles):
        return canon(template_esmiles)
    # graph_substitute expects the <a>idx:label</a> annotations to match enum keys.
    try:
        filled = graph_substitute(template_esmiles, dict(r_dict))
        out = filled[0] if isinstance(filled, tuple) else filled
        if isinstance(out, str) and "*" not in out:
            c = canon(out)
            if c:
                return c
    except Exception:
        pass
    return _mm_inject_fill(template_esmiles, r_dict)


def _mm_inject_fill(template_esmiles: str, r_dict: Dict[str, str]) -> Optional[str]:
    """Fallback 带入 via MarkushMap dict-injection (runbook/19)."""
    try:
        from utils import get_abbrev_smi  # Translator/utils singleton
        from mapping_result import fill_general_smiles_with_r_group_dict
    except Exception:
        return None
    try:
        abbr = get_abbrev_smi()
        rgd: Dict[str, List[str]] = {}
        for i, (label, frag) in enumerate(r_dict.items()):
            name = f"_p1zq{i}"
            abbr[name] = frag.lstrip("*")
            rgd[label] = [name]
        out = fill_general_smiles_with_r_group_dict(template_esmiles, rgd)
        if out:
            return canon(out[0])
    except Exception:
        return None
    return None


# --- UniParser object parsing -------------------------------------------------
_MOL_RE = re.compile(r"\\begin\{molecule\}\s*(.*?)\s*\\end\{molecule\}", re.DOTALL)
_RXN_RE = re.compile(r"\\begin\{reaction\}\s*(.*?)\s*\\end\{reaction\}", re.DOTALL)
_MID_RE = re.compile(r"\\begin\{moleculeid\}\s*(.*?)\s*\\end\{moleculeid\}", re.DOTALL)


class UObj:
    __slots__ = ("kind", "payload", "bbox", "order", "raw")

    def __init__(self, kind, payload, bbox, order, raw):
        self.kind = kind
        self.payload = payload
        self.bbox = bbox
        self.order = order
        self.raw = raw


def parse_objects(raw_result: Dict[str, Any]) -> List[UObj]:
    """Parse result['objects'] into ordered typed UObj list."""
    out: List[UObj] = []
    objs = raw_result.get("objects") or []
    for i, o in enumerate(objs):
        if not isinstance(o, dict):
            continue
        cls = o.get("class")
        s = o.get("str") or ""
        bbox = o.get("float_xyxy")
        if cls == "molecule":
            m = _MOL_RE.search(s)
            esmi = m.group(1).strip() if m else ""
            out.append(UObj("molecule", esmi, bbox, i, o))
        elif cls == "expression":
            m = _RXN_RE.search(s)
            rxns = []
            if m:
                body = m.group(1).strip()
                try:
                    parsed = ast.literal_eval(body)
                    if isinstance(parsed, list):
                        rxns = [r for r in parsed if isinstance(r, dict)]
                except Exception:
                    rxns = []
            out.append(UObj("expression", rxns, bbox, i, o))
        elif cls in ("moleculeid", "identifier"):
            m = _MID_RE.search(s)
            txt = (m.group(1).strip() if m else s.strip())
            out.append(UObj("yield", txt, bbox, i, o))
        # everything else (image/hline/paragraph/figurecaption/...) is ignored
    return out


_YIELD_RE = re.compile(r"(\d{1,3})\s*%")


def parse_yield(txt: str) -> Optional[str]:
    """'78%o yield' / '58%0 yield' -> '78%' (OCR-noise tolerant)."""
    if not txt:
        return None
    m = _YIELD_RE.search(txt)
    return f"{m.group(1)}%" if m else None


# --- mol-block + condition helpers (match finalize/score shapes) --------------
def mol_block(esmiles: str, identifier: str = "", source_type: str = "image_structure",
              extra: Optional[dict] = None) -> dict:
    src = {"type": source_type}
    if extra:
        src.update(extra)
    return {"identifier": identifier, "E-SMILES": esmiles, "_source": src}


def wrap_conditions(cond: Optional[dict]) -> list:
    """UniParser reaction.condition -> V2 list-of-list of {role,text,_source}."""
    if not cond:
        return []
    entries = []
    text = (cond.get("text") or "").strip()
    if text:
        entries.append({"role": "reagent", "text": text,
                        "_source": {"type": "image_ocr"}})
    for cat in cond.get("catalyst") or []:
        if cat:
            entries.append({"role": "catalyst", "text": cat,
                            "_source": {"type": "image_ocr"}})
    return [entries] if entries else []


def with_sep(smiles: str) -> str:
    """Emit E-SMILES with the GT trailing <sep> convention."""
    if not smiles:
        return smiles
    return smiles if "<sep>" in smiles else f"{smiles}<sep>"


# --- core adapter -------------------------------------------------------------
def adapt_figure(raw_result: Dict[str, Any], meta: Optional[dict] = None,
                 salvage_product_only: bool = True) -> dict:
    meta = meta or {}
    objs = parse_objects(raw_result)

    expressions = [o for o in objs if o.kind == "expression"]

    templates: List[dict] = []
    examples: List[dict] = []
    stats = {"n_expr": len(expressions), "n_template_rxns": 0, "n_concrete_rxns": 0,
             "n_examples_engine": 0, "n_examples_product_only": 0, "n_mol_objs": 0}

    # (1) reactions from expressions -> templates / concrete reactions ---------
    template_reactions: List[Tuple[List[str], List[str], Optional[dict]]] = []
    tid = 0
    for ex in expressions:
        for rxn in ex.payload:
            prev = [p for p in (rxn.get("prev_mols") or []) if p]
            post = [p for p in (rxn.get("post_mols") or []) if p]
            cond = rxn.get("condition")
            if not prev and not post:
                continue
            markush = any(is_markush(x) for x in prev + post)
            if markush:
                tid += 1
                templates.append({
                    "template_id": f"T{tid}",
                    "reactants": [mol_block(with_sep(p), f"r{i+1}",
                                            "image_structure" if is_markush(p) else "image_structure")
                                  for i, p in enumerate(prev)],
                    "products": [mol_block(with_sep(p), f"p{i+1}") for i, p in enumerate(post)],
                    "conditions": wrap_conditions(cond),
                    "metrics": [],
                    "_source": {"type": "uniparser_expression"},
                })
                template_reactions.append((prev, post, cond))
                stats["n_template_rxns"] += 1
            else:
                # concrete reaction drawn directly -> emit as a selected_example
                c_reac = [canon(p) for p in prev]
                c_prod = [canon(p) for p in post]
                c_reac = [x for x in c_reac if x]
                c_prod = [x for x in c_prod if x]
                if c_reac or c_prod:
                    examples.append({
                        "instance_id": f"rxn_concrete_{len(examples)+1}",
                        "inherit_template": "",
                        "markush_enumeration": {},
                        "reactants": [mol_block(with_sep(x)) for x in c_reac],
                        "products": [mol_block(with_sep(x)) for x in c_prod],
                        "conditions": wrap_conditions(cond),
                        "metrics": [],
                        "_source": {"type": "uniparser_expression_concrete"},
                    })
                    stats["n_concrete_rxns"] += 1

    # (2) concrete example products: molecule immediately followed by a yield ---
    # Build (concrete_product_smiles, yield, order) list.
    example_products: List[Tuple[str, Optional[str], int]] = []
    for idx, o in enumerate(objs):
        if o.kind != "molecule":
            continue
        stats["n_mol_objs"] += 1
        esmi = o.payload
        if not esmi or is_markush(esmi):
            continue  # template-like or empty; not a concrete product candidate
        c = canon(esmi)
        if not c:
            continue
        # look ahead for an adjacent yield (within next 2 objects)
        yld = None
        for nxt in objs[idx + 1: idx + 3]:
            if nxt.kind == "yield":
                yld = parse_yield(nxt.payload)
                break
            if nxt.kind == "molecule":
                break
        example_products.append((c, yld, o.order))

    # (3) per concrete product: assign best template, 反推 -> 带入 -------------
    # STRICT: only emit derived reactants when the round-trip VERIFIES
    # (带入 R back into the template product == the concrete product). An
    # unverified match means UniParser's template perception is wrong (e.g.
    # paper49/fig2 garbled expression template) — injecting its reactants would
    # tank precision, so those fall through to product-only / skip.
    def best_template_and_R(prod_smiles: str):
        """Return (template_index, r_dict) for the template whose product 反推
        recovers R that round-trips to prod_smiles, else (None, {})."""
        for ti, (prev, post, _c) in enumerate(template_reactions):
            tmpl_prod = next((p for p in post if is_markush(p)), None)
            if not tmpl_prod:
                continue
            r = reverse_r(tmpl_prod, prod_smiles)
            if not r:
                continue
            recon = forward_fill(tmpl_prod, r)
            if recon and recon == prod_smiles:
                return ti, r
        return (None, {})

    for pi, (prod_smiles, yld, order) in enumerate(example_products):
        ti, r = best_template_and_R(prod_smiles)
        metrics = [{"role": "yield", "text": yld, "of": f"ex{pi+1}"}] if yld else []
        if ti is not None and r:
            prev, post, cond = template_reactions[ti]
            tmpl = templates[ti]
            reactant_blocks = []
            for tr in prev:
                if is_markush(tr):
                    filled = forward_fill(tr, r)
                    reactant_blocks.append(
                        mol_block(with_sep(filled if filled else strip_esmiles(tr)),
                                  source_type="reverse_fill"
                                  if filled else "unresolved_markush"))
                else:
                    c = canon(tr)
                    if c:
                        reactant_blocks.append(mol_block(with_sep(c)))
            examples.append({
                "instance_id": f"ex{pi+1}",
                "inherit_template": tmpl["template_id"],
                "markush_enumeration": {k: v for k, v in r.items()},
                "reactants": reactant_blocks,
                "products": [mol_block(with_sep(prod_smiles), source_type="image_structure")],
                "conditions": wrap_conditions(cond),
                "metrics": metrics,
                "_source": {"type": "uniparser_enumerated", "engine": "from_mapping+graph_substitute"},
            })
            stats["n_examples_engine"] += 1
        elif salvage_product_only:
            # 反推 failed: emit product-only (recall salvage at Jaccard 0.5 for mono-reactant GT)
            examples.append({
                "instance_id": f"ex{pi+1}",
                "inherit_template": "",
                "markush_enumeration": {},
                "reactants": [],
                "products": [mol_block(with_sep(prod_smiles), source_type="image_structure")],
                "conditions": [],
                "metrics": metrics,
                "_source": {"type": "uniparser_product_only",
                            "note": "reverse-derivation failed; reactant not resolved"},
            })
            stats["n_examples_product_only"] += 1

    out = {
        "schema_version": "v2",
        "task_id": meta.get("task_id", ""),
        "paper_id": meta.get("paper_id", ""),
        "anchor_image": meta.get("anchor_image", ""),
        "anchor_figure": meta.get("anchor_figure", ""),
        "anchor_page": meta.get("anchor_page", ""),
        "extractor": "uniparser_to_v2",
        "molparser_detection_count": stats["n_mol_objs"],
        "identifier_map": {},
        "reaction_templates": templates,
        "selected_examples": examples,
        "is_multistep_synthesis": False,
        "synthesis_overview": None,
        "reaction_graph": [],
        "_p1_stats": stats,
    }
    return out


# --- CLI ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="UniParser raw JSON -> V2 figure JSON")
    ap.add_argument("raw_json", help="UniParser get_result(objects=True) JSON for one figure")
    ap.add_argument("-o", "--out", help="output V2 JSON path (default: stdout)")
    ap.add_argument("--no-salvage", action="store_true",
                    help="do NOT emit product-only examples when 反推 fails")
    ap.add_argument("--meta", help="optional JSON string of figure metadata")
    args = ap.parse_args()

    raw = json.load(open(args.raw_json))
    meta = json.loads(args.meta) if args.meta else {}
    v2 = adapt_figure(raw, meta, salvage_product_only=not args.no_salvage)
    txt = json.dumps(v2, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(txt)
        print(f"[uniparser_to_v2] wrote {args.out}  stats={v2['_p1_stats']}")
    else:
        print(txt)


if __name__ == "__main__":
    main()
