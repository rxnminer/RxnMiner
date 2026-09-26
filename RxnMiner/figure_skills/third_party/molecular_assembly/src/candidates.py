"""Build and dedupe Step0 match candidates."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from rdkit import Chem

from anchors import template_dummy_to_rlabel, peeled_dummy_to_rlabel
from align import check_mapped_bond_orders
from assembly import peel_scaffold_mol_for_topology_check
from esmiles_contract import (
    AttachmentContract,
    VirtualArcCheckResult,
    validate_virtual_arcs_for_alignment,
    virtual_arc_zone_template_atoms,
)
from models import AnchorInfo, MatchCandidate, SOURCE_SORT_KEY
from peel import peel_substituents
from peel_topology import (
    check_peeled_duplicate_label_substituents,
    check_peeled_topology_matches_template,
)
from roundtrip import LibraryContext, verify_library_match, verify_roundtrip
from stereo import check_mapped_stereo, check_mapped_stereo_peeled


def candidate_full_pass(c: MatchCandidate) -> bool:
    """全通过：键级 + 虚拟弧（若有）+ 扣后结构（Ts+同位素 InChIKey / canonical，含立体层）。"""
    return c.bond_order_ok and c.virtual_arc_ok and c.peel_topology_ok


def candidate_roundtrip_pass(c: MatchCandidate) -> bool:
    """键级 + 扣后结构 + 组装回目标（InChIKey 含立体）。"""
    return candidate_full_pass(c) and c.roundtrip_ok


def t2f_fingerprint(template_to_full: dict[str, int] | dict[int, int]) -> frozenset[tuple[int, int]]:
    """Canonical key for template_idx -> full_idx (same index map = duplicate)."""
    pairs: list[tuple[int, int]] = []
    for t_idx, f_idx in template_to_full.items():
        pairs.append((int(t_idx), int(f_idx)))
    return frozenset(pairs)


def candidate_sort_key(c: MatchCandidate) -> tuple[Any, ...]:
    return (
        not c.bond_order_ok,
        c.bond_mismatch_count,
        not c.virtual_arc_ok,
        not c.peel_topology_ok,
        not c.peel_parity_ok,          # 几何奇偶性更可靠，提上来
        c.peel_parity_mismatches,
        -c.mapped_atoms,               # 映射原子数越多越好
        c.index
    )


def highlight_fingerprint(template_to_full: dict[str, int] | dict[int, int]) -> frozenset[int]:
    """Product atoms highlighted in the figure — same set => same picture."""
    return frozenset(int(v) for v in template_to_full.values())


def merge_into_candidate(prev: MatchCandidate, c: MatchCandidate) -> MatchCandidate:
    also = list(prev.also_sources)
    if c.source not in also:
        also.append(c.source)
    merged = prev.merged_map_count + 1
    winner = c if candidate_sort_key(c) < candidate_sort_key(prev) else prev
    return replace(winner, also_sources=also, merged_map_count=merged)


def dedupe_by_index_map(
    candidates: list[MatchCandidate],
) -> list[MatchCandidate]:
    """One row per distinct template_idx→full_idx (index-table) map."""
    by_key: dict[frozenset[tuple[int, int]], MatchCandidate] = {}
    for c in candidates:
        key = t2f_fingerprint(c.template_to_full)
        prev = by_key.get(key)
        if prev is None:
            c.also_sources = [c.source]
            c.merged_map_count = 1
            by_key[key] = c
            continue
        by_key[key] = merge_into_candidate(prev, c)
    out = sorted(by_key.values(), key=candidate_sort_key)
    for i, c in enumerate(out):
        c.index = i
    return out


def dedupe_match_candidates_for_display(
    candidates: list[MatchCandidate],
) -> list[MatchCandidate]:
    """One row per distinct highlight on target (visual); best priority wins."""
    by_visual: dict[frozenset[int], MatchCandidate] = {}
    for c in candidates:
        key = highlight_fingerprint(c.template_to_full)
        prev = by_visual.get(key)
        if prev is None:
            c.also_sources = [c.source]
            c.merged_map_count = 1
            by_visual[key] = c
            continue
        by_visual[key] = merge_into_candidate(prev, c)
    out = sorted(by_visual.values(), key=candidate_sort_key)
    for i, c in enumerate(out):
        c.index = i
    return out


def _dedupe_template_to_full_maps(maps: list[dict[int, int]]) -> list[dict[int, int]]:
    seen: set[frozenset[tuple[int, int]]] = set()
    out: list[dict[int, int]] = []
    for t2f in maps:
        key = frozenset(t2f.items())
        if key in seen:
            continue
        seen.add(key)
        out.append(t2f)
    return out


def _enumerate_substructure_alignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    *,
    uniquify: bool = False,
) -> list[dict[int, int]]:
    """B: template heavy-atom subgraph embedded in target (keeps *-disconnect topology)."""
    query, _ = _template_heavy_query_mol(template_mol)
    if query is None:
        return []
    nq = query.GetNumAtoms()
    t_refs = template_mol.GetSubstructMatches(query, uniquify=uniquify)
    f_refs = target_mol.GetSubstructMatches(query, uniquify=uniquify)
    if not t_refs or not f_refs:
        return []

    raw: list[dict[int, int]] = []
    for t_ref in t_refs:
        if len(t_ref) != nq:
            continue
        for f_ref in f_refs:
            if len(f_ref) != nq:
                continue
            raw.append({int(t_ref[k]): int(f_ref[k]) for k in range(nq)})
    return _dedupe_template_to_full_maps(raw)


def _enumerate_mcs_alignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    *,
    bond_compare: Any,
    uniquify: bool = False,
) -> tuple[list[dict[int, int]], int]:
    """A: MCS embedding; returns maps and MCS atom count."""
    try:
        res = rdFMCS.FindMCS(
            [template_mol, target_mol],
            atomCompare=rdFMCS.AtomCompare.CompareElements,
            bondCompare=bond_compare,
            completeRingsOnly=False,
            ringMatchesRingOnly=False,
        )
    except Exception:
        return [], 0
    if not res.smartsString:
        return [], 0
    mcs = Chem.MolFromSmarts(res.smartsString)
    if mcs is None:
        return [], 0

    mcs_atoms = mcs.GetNumAtoms()
    q_hits = template_mol.GetSubstructMatches(mcs, uniquify=uniquify)
    t_hits = target_mol.GetSubstructMatches(mcs, uniquify=uniquify)
    if not q_hits or not t_hits:
        return [], mcs_atoms

    raw: list[dict[int, int]] = []
    for q_match in q_hits:
        for t_match in t_hits:
            if len(q_match) != len(t_match):
                continue
            raw.append(
                {int(q_match[k]): int(t_match[k]) for k in range(len(q_match))}
            )
    return _dedupe_template_to_full_maps(raw), mcs_atoms


def _enumerate_topology_alignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
) -> list[dict[int, int]]:
    """Legacy alias: MCS CompareAny, uniquify=False."""
    maps, _ = _enumerate_mcs_alignments(
        template_mol,
        target_mol,
        bond_compare=rdFMCS.BondCompare.CompareAny,
        uniquify=False,
    )
    return maps


def _collect_all_alignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
) -> list[tuple[str, dict[int, int], dict[str, Any]]]:
    """Run B + A (order + topology); return (source_label, t2f, meta)."""
    out: list[tuple[str, dict[int, int], dict[str, Any]]] = []

    b_maps = _enumerate_substructure_alignments(
        template_mol, target_mol, uniquify=False
    )
    for t2f in b_maps:
        out.append(("B·子结构", t2f, {"query_atoms": len(t2f)}))

    order_maps, mcs_n = _enumerate_mcs_alignments(
        template_mol,
        target_mol,
        bond_compare=rdFMCS.BondCompare.CompareOrder,
        uniquify=False,
    )
    for t2f in order_maps:
        out.append(("A·MCS键级", t2f, {"mcs_atoms": mcs_n}))

    any_maps, mcs_n_any = _enumerate_mcs_alignments(
        template_mol,
        target_mol,
        bond_compare=rdFMCS.BondCompare.CompareAny,
        uniquify=False,
    )
    for t2f in any_maps:
        out.append(("A·MCS拓扑", t2f, {"mcs_atoms": mcs_n_any}))

    return out


def mol_highlight_png(
    mol: Chem.Mol,
    highlight_atoms: list[int],
    highlight_bonds: list[int] | None = None,
    size: tuple[int, int] = (520, 360),
) -> str:
    from rdkit.Chem import Draw

    import base64
    import io

    kwargs: dict[str, Any] = {
        "size": size,
        "highlightAtoms": highlight_atoms or [],
    }
    if highlight_bonds:
        kwargs["highlightBonds"] = highlight_bonds
    img = Draw.MolToImage(mol, **kwargs)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _virtual_arc_check_dicts(
    checks: list[VirtualArcCheckResult],
) -> tuple[bool, str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for ch in checks:
        rows.append(
            {
                "arc_index": ch.arc_index,
                "name": ch.name,
                "from_position": ch.from_position,
                "to_position": ch.to_position,
                "template_from": ch.template_from,
                "template_to": ch.template_to,
                "full_from": ch.full_from,
                "full_to": ch.full_to,
                "satisfied": ch.satisfied,
                "mode": ch.mode,
                "note": ch.note,
            }
        )
    if not checks:
        return True, "", rows
    ok = all(ch.satisfied for ch in checks)
    note = (
        ""
        if ok
        else "虚拟弧未满足: "
        + ", ".join(f"{ch.name}({ch.mode})" for ch in checks if not ch.satisfied)
    )
    return ok, note, rows


def build_candidate(
    *,
    index: int,
    source: str,
    t2f_int: dict[int, int],
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    anchors: list[AnchorInfo],
    render_images: bool = True,
    target_smiles: str = "",
    library: LibraryContext | None = None,
    attachment_contract: AttachmentContract | None = None,
    atom_by_position: list[int] | None = None,
) -> MatchCandidate:
    virtual_arcs = (
        attachment_contract.virtual_arcs if attachment_contract is not None else []
    )
    arc_zone = (
        virtual_arc_zone_template_atoms(
            template_mol, virtual_arcs, atom_by_position or []
        )
        if virtual_arcs
        else set()
    )
    bond_ok, bond_mm = check_mapped_bond_orders(
        template_mol,
        target_mol,
        t2f_int,
        arc_zone_atoms=arc_zone,
    )
    stereo_ok, stereo_mm = check_mapped_stereo(template_mol, target_mol, t2f_int)

    arc_ok, arc_checks = validate_virtual_arcs_for_alignment(
        target_mol,
        t2f_int,
        virtual_arcs,
        atom_by_position or [],
    )
    virtual_arc_ok, virtual_arc_note, virtual_arc_dicts = _virtual_arc_check_dicts(
        arc_checks
    )
    t2f = {str(k): v for k, v in t2f_int.items()}
    anchor_rows: list[dict[str, Any]] = []
    for a in anchors:
        anchor_rows.append(
            {
                "r_label": a.r_label,
                "internal_id": a.internal_id,
                "display_label": a.display_label,
                "pair_key": a.pair_key,
                "occurrence": a.occurrence,
                "template_dummy_idx": a.template_dummy_idx,
                "template_anchor_idx": a.template_anchor_idx,
                "bridge_anchor_idxs": list(a.bridge_anchor_idxs),
                "anchor_kind": a.anchor_kind,
                "full_anchor_idx": t2f_int.get(a.template_anchor_idx, -1),
                "heavy_degree": a.heavy_degree,
                "anchor_symbol": a.anchor_symbol,
            }
        )
    mismatch_dicts = [
        {
            "template_i": m.template_i,
            "template_j": m.template_j,
            "full_i": m.full_i,
            "full_j": m.full_j,
            "template_bond": m.template_bond,
            "full_bond": m.full_bond,
        }
        for m in bond_mm
    ]
    stereo_dicts = [
        {
            "template_idx": m.template_idx,
            "full_idx": m.full_idx,
            "template_stereo": m.template_stereo,
            "full_stereo": m.full_stereo,
            "note": m.note,
        }
        for m in stereo_mm
    ]
    highlight_atoms = list(t2f_int.values())
    highlight_bonds: list[int] = []
    for m in bond_mm:
        b = target_mol.GetBondBetweenAtoms(m.full_i, m.full_j)
        if b is not None:
            highlight_bonds.append(b.GetIdx())

    peel = peel_substituents(
        target_mol, template_mol, t2f_int, anchors, render_images=render_images
    )
    template_dummy_map = template_dummy_to_rlabel(anchors)
    peeled_dummy_map: dict[int, str] = {}
    peel_mol_for_topo: Chem.Mol | None = None
    if peel.scaffold_mol is not None:
        peel_mol_for_topo = peel_scaffold_mol_for_topology_check(
            peel.scaffold_mol,
            template_mol=template_mol,
            template_to_full=t2f_int,
        )
        peeled_dummy_map = peeled_dummy_to_rlabel(
            peel.scaffold_mol, anchors, t2f_int
        )
    _topo = check_peeled_topology_matches_template(
        template_mol,
        peel.scaffold_smiles,
        template_dummy_to_rlabel=template_dummy_map,
        peel_mol=peel_mol_for_topo,
        peeled_dummy_to_rlabel=peeled_dummy_map,
    )
    peel_topology_ok, peel_topology_note = _topo.ok, _topo.note
    peel_topology_inchi_t, peel_topology_inchi_p = _topo.inchi_key_template, _topo.inchi_key_peeled
    peel_stereo_ok, peel_stereo_mm, peel_chiral = check_mapped_stereo_peeled(
        template_mol, peel.scaffold_mol, t2f_int
    )
    peel_stereo_dicts = [
        {
            "template_idx": m.template_idx,
            "peeled_idx": m.full_idx,
            "template_stereo": m.template_stereo,
            "peeled_stereo": m.full_stereo,
            "note": m.note,
        }
        for m in peel_stereo_mm
    ]
    peeled_rows = [
        {
            "r_label": p.r_label,
            "internal_id": p.internal_id or p.display_label,
            "display_label": p.display_label,
            "slot_ids": p.slot_ids,
            "anchor_full_idx": p.anchor_full_idx,
            "anchor_full_idxs": p.anchor_full_idxs,
            "smiles": p.smiles,
            "heavy_atoms": p.heavy_atoms,
            "image_base64": p.image_base64,
        }
        for p in peel.substituents
    ]
    dup_peel = check_peeled_duplicate_label_substituents(anchor_rows, peeled_rows)

    roundtrip = verify_roundtrip(
        target_smiles or Chem.MolToSmiles(target_mol),
        peel,
        anchors,
        t2f_int,
        render_images=render_images,
    )
    lib = verify_library_match(peel.substituents, library)

    return MatchCandidate(
        index=index,
        source=source,
        template_to_full=t2f,
        anchors=anchor_rows,
        mapped_atoms=len(t2f_int),
        bond_order_ok=bond_ok,
        bond_mismatch_count=len(bond_mm),
        bond_mismatches=mismatch_dicts,
        stereo_ok=stereo_ok,
        stereo_mismatch_count=len(stereo_mm),
        stereo_mismatches=stereo_dicts,
        peel_topology_ok=peel_topology_ok,
        peel_topology_note=peel_topology_note,
        peel_topology_inchi_template=peel_topology_inchi_t,
        peel_topology_inchi_peeled=peel_topology_inchi_p,
        peel_stereo_ok=peel_stereo_ok,
        peel_stereo_mismatch_count=len(peel_stereo_mm),
        peel_stereo_mismatches=peel_stereo_dicts,
        peel_chiral_centers=peel_chiral,
        peel_parity_ok=peel.peel_parity_ok,
        peel_parity_mismatches=peel.peel_parity_mismatches,
        highlight_image_base64=(
            mol_highlight_png(
                target_mol,
                highlight_atoms,
                highlight_bonds if highlight_bonds else None,
            )
            if render_images
            else ""
        ),
        labeled_image_base64=peel.target_labeled_image_base64,
        peel_scaffold_smiles=peel.scaffold_smiles,
        peel_scaffold_image_base64=peel.scaffold_image_base64,
        peeled_substituents=peeled_rows,
        roundtrip_ok=roundtrip.ok,
        roundtrip_note=roundtrip.note,
        assembled_smiles=roundtrip.assembled_smiles,
        assembled_inchi_key=roundtrip.assembled_inchi_key,
        target_inchi_key=roundtrip.target_inchi_key,
        assembled_image_base64=roundtrip.assembled_image_base64,
        assembly_messages=roundtrip.assembly_messages,
        assembly_fragment_lines=roundtrip.assembly_fragment_lines,
        library_ok=lib.ok,
        library_notes=lib.notes,
        virtual_arc_ok=virtual_arc_ok,
        virtual_arc_note=virtual_arc_note,
        virtual_arc_checks=virtual_arc_dicts,
        duplicate_label_peel_warn=dup_peel.warn,
        duplicate_label_peel_groups=dup_peel.mismatch_groups,
        duplicate_label_peel_note=dup_peel.note,
    )
