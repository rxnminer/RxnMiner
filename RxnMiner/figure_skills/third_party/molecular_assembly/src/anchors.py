"""Template anchor slots from <a> tags and extended eSMILES contracts."""
from __future__ import annotations

from rdkit import Chem

from assembly import _CONNECTABLE_BOND_TYPES
from chem_util import heavy_degree
from esmiles_contract import (
    AttachmentContract,
    build_attachment_contract,
    contract_slot_from_label,
    pick_ring_substituent_anchor,
)
from esmiles_loader import EsmilesLoadResult, atom_id_by_position
from models import AnchorInfo
from peel_topology import is_attachment_placeholder
from r_groups import RTagSlot, normalize_r_tags, pair_key_from_source, slot_key_from_label


def dummy_attachment_bond_type(mol: Chem.Mol, dummy_idx: int, anchor_idx: int) -> Chem.BondType:
    """Bond type at template *–anchor (same rule as assembly.build_complete_molecule)."""
    bond = mol.GetBondBetweenAtoms(dummy_idx, anchor_idx)
    if bond is None:
        return Chem.BondType.SINGLE
    bt = bond.GetBondType()
    if bt in _CONNECTABLE_BOND_TYPES:
        return bt
    return Chem.BondType.SINGLE


def anchor_connection_bond_type(
    mol: Chem.Mol,
    anchor: AnchorInfo,
    *,
    subst_neighbor_idx: int = -1,
) -> Chem.BondType:
    """Bond type at peel boundary for dummy, ring, or virtual-arc anchors."""
    if anchor.template_dummy_idx >= 0:
        return dummy_attachment_bond_type(
            mol, anchor.template_dummy_idx, anchor.template_anchor_idx
        )
    ai = anchor.template_anchor_idx
    if subst_neighbor_idx >= 0:
        bond = mol.GetBondBetweenAtoms(ai, subst_neighbor_idx)
        if bond is not None:
            bt = bond.GetBondType()
            if bt in _CONNECTABLE_BOND_TYPES:
                return bt
    for n in mol.GetAtomWithIdx(ai).GetNeighbors():
        if n.GetAtomicNum() > 1:
            bond = mol.GetBondBetweenAtoms(ai, n.GetIdx())
            if bond is not None:
                bt = bond.GetBondType()
                if bt in _CONNECTABLE_BOND_TYPES:
                    return bt
    return Chem.BondType.SINGLE


def outside_anchor_neighbors(
    target_mol: Chem.Mol,
    anchor_full_idx: int,
    core_full: set[int],
) -> list[int]:
    """Heavy neighbors of anchor that are not in the mapped scaffold core."""
    out: list[int] = []
    for n in target_mol.GetAtomWithIdx(anchor_full_idx).GetNeighbors():
        if n.GetAtomicNum() > 1 and n.GetIdx() not in core_full:
            out.append(n.GetIdx())
    return out


def template_anchor_has_explicit_h(template_mol: Chem.Mol, anchor_idx: int) -> bool:
    return any(
        n.GetAtomicNum() == 1
        for n in template_mol.GetAtomWithIdx(anchor_idx).GetNeighbors()
    )


def template_dummy_to_rlabel(anchors: list[AnchorInfo]) -> dict[int, str]:
    return {a.template_dummy_idx: a.r_label for a in anchors if a.template_dummy_idx >= 0}


def template_dummy_slot_notes(anchors: list[AnchorInfo]) -> dict[int, str]:
    """Map template ``*`` atom idx → ``<a>`` label (short form) for ``atomNote`` drawing."""
    return {a.template_dummy_idx: a.display_label for a in anchors if a.template_dummy_idx >= 0}


def template_anchor_indices(anchor: AnchorInfo) -> tuple[int, ...]:
    """All template-side heavy atoms represented by an anchor."""
    if anchor.bridge_anchor_idxs:
        return anchor.bridge_anchor_idxs
    return (anchor.template_anchor_idx,)


def peeled_dummy_to_rlabel(
    scaffold_mol: Chem.Mol,
    anchors: list[AnchorInfo],
    template_to_full: dict[int, int],
) -> dict[int, str]:
    """Map each Ts placeholder on peeled scaffold (by idx) to Synfacts source label."""
    from r_groups import placeholder_isotope_from_r_label

    iso_to_rlabel = {
        placeholder_isotope_from_r_label(a.r_label): a.r_label for a in anchors
    }
    out: dict[int, str] = {}
    for atom in scaffold_mol.GetAtoms():
        if not is_attachment_placeholder(atom):
            continue
        iso = atom.GetIsotope()
        if iso in iso_to_rlabel:
            out[atom.GetIdx()] = iso_to_rlabel[iso]
    if out:
        return out
    for a in anchors:
        orig_anchor = template_to_full.get(a.template_anchor_idx, -1)
        if orig_anchor < 0:
            continue
        _anch, dummy_idx = attachment_anchor_and_dummy(scaffold_mol, orig_anchor)
        if dummy_idx is not None:
            out[dummy_idx] = a.r_label
    return out


def attachment_anchor_and_dummy(mol: Chem.Mol, orig_anchor_idx: int) -> tuple[int | None, int | None]:
    """After build_scaffold_with_dummies, map original anchor idx → (anchor, *)."""
    orig_to_new: dict[int, int] = {}
    for a in mol.GetAtoms():
        if a.HasProp("_origIdx"):
            orig_to_new[int(a.GetProp("_origIdx"))] = a.GetIdx()
    anchor_new = orig_to_new.get(orig_anchor_idx)
    if anchor_new is None:
        return None, None
    dummy_new: int | None = None
    for n in mol.GetAtomWithIdx(anchor_new).GetNeighbors():
        if is_attachment_placeholder(n):
            dummy_new = n.GetIdx()
            break
    return anchor_new, dummy_new


def parse_anchors(template_mol: Chem.Mol, tags: str) -> list[AnchorInfo]:
    return anchors_from_slots(template_mol, normalize_r_tags(tags))


def anchors_from_slots(template_mol: Chem.Mol, slots: list[RTagSlot]) -> list[AnchorInfo]:
    anchors: list[AnchorInfo] = []
    for slot in slots:
        dummy_idx = slot.dummy_idx
        dummy = template_mol.GetAtomWithIdx(dummy_idx)
        if not is_attachment_placeholder(dummy):
            continue
        nbrs = [n for n in dummy.GetNeighbors() if n.GetAtomicNum() > 1]
        if not nbrs:
            continue
        anchor_idxs = tuple(n.GetIdx() for n in nbrs)
        anchor = nbrs[0]
        ai = anchor_idxs[0]
        kind = "bridge" if len(anchor_idxs) > 1 else "dummy"
        anchors.append(
            AnchorInfo(
                template_dummy_idx=dummy_idx,
                template_anchor_idx=ai,
                r_label=slot.source_label,
                internal_id=slot.internal_id,
                display_label=slot.display_label,
                pair_key=slot.pair_key,
                occurrence=slot.occurrence,
                heavy_degree=heavy_degree(template_mol, ai),
                anchor_symbol=anchor.GetSymbol(),
                smiles_position=slot.smiles_position,
                anchor_kind=kind,
                bridge_anchor_idxs=anchor_idxs if kind == "bridge" else (),
            )
        )
    return anchors


def anchors_from_contract(
    template_mol: Chem.Mol,
    contract: AttachmentContract,
    atom_by_position: list[int],
    *,
    existing: list[AnchorInfo] | None = None,
) -> list[AnchorInfo]:
    """Merge dummy ``<a>`` anchors with ``<r>`` ring and ``<v>`` virtual-arc ports."""
    anchors: list[AnchorInfo] = list(existing or [])
    used_anchor_idxs = {idx for a in anchors for idx in template_anchor_indices(a)}

    ring_seen: dict[str, int] = {}

    for ring_tag in contract.ring_substituents:
        pk = pair_key_from_source(ring_tag.label)
        ring_seen[pk] = ring_seen.get(pk, 0) + 1
        occ = ring_seen[pk]
        picked = pick_ring_substituent_anchor(
            template_mol,
            ring_tag.ring_index,
            exclude_positions=used_anchor_idxs,
        )
        if picked is None:
            continue
        ring_anchor, _side = picked
        if ring_anchor in used_anchor_idxs:
            continue
        sk = slot_key_from_label(ring_tag.label, occ)
        anchor_atom = template_mol.GetAtomWithIdx(ring_anchor)
        anchors.append(
            AnchorInfo(
                template_dummy_idx=-1,
                template_anchor_idx=ring_anchor,
                r_label=ring_tag.label,
                internal_id=sk,
                display_label=contract_slot_from_label(
                    ring_tag.label,
                    dummy_idx=-1,
                    smiles_position=-1,
                    occurrence=occ,
                ).display_label,
                pair_key=pk,
                occurrence=occ,
                heavy_degree=heavy_degree(template_mol, ring_anchor),
                anchor_symbol=anchor_atom.GetSymbol(),
                smiles_position=-1,
                anchor_kind="ring",
            )
        )
        used_anchor_idxs.add(ring_anchor)

    arc_occ: dict[str, int] = {}
    for arc in contract.virtual_arcs:
        from_idx = atom_id_by_position(atom_by_position, arc.from_position)
        to_idx = atom_id_by_position(atom_by_position, arc.to_position)
        if from_idx < 0 or to_idx < 0:
            continue
        endpoints = tuple(dict.fromkeys((from_idx, to_idx)))
        for endpoint, pos in ((from_idx, arc.from_position), (to_idx, arc.to_position)):
            if endpoint in used_anchor_idxs:
                continue
            arc_occ[arc.name] = arc_occ.get(arc.name, 0) + 1
            occ = arc_occ[arc.name]
            sk = slot_key_from_label(arc.name, occ)
            atom = template_mol.GetAtomWithIdx(endpoint)
            anchors.append(
                AnchorInfo(
                    template_dummy_idx=-1,
                    template_anchor_idx=endpoint,
                    r_label=arc.name,
                    internal_id=sk,
                    display_label=arc.name,
                    pair_key=arc.name,
                    occurrence=occ,
                    heavy_degree=heavy_degree(template_mol, endpoint),
                    anchor_symbol=atom.GetSymbol(),
                    smiles_position=pos,
                    anchor_kind="virtual_arc",
                    secondary_anchor_idx=to_idx if endpoint == from_idx else from_idx,
                    virtual_arc_index=arc.arc_index,
                    bridge_anchor_idxs=endpoints,
                )
            )
            used_anchor_idxs.add(endpoint)

    return anchors


def anchors_from_esmiles(
    template_mol: Chem.Mol,
    loaded: EsmilesLoadResult,
) -> list[AnchorInfo]:
    """Full semantic anchors: ``<a>`` dummies + ``<r>`` ring + ``<v>`` arc ports."""
    dummy_anchors = anchors_from_slots(template_mol, loaded.anchor_slots)
    contract = loaded.attachment_contract
    if contract is None:
        contract = build_attachment_contract(
            loaded.info, loaded.anchor_slots, loaded.v_tags
        )
    return anchors_from_contract(
        template_mol,
        contract,
        loaded.atom_by_position,
        existing=dummy_anchors,
    )
