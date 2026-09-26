"""Template anchor slots from <a> tags and extended eSMILES contracts."""

from __future__ import annotations

from itertools import combinations

from rdkit import Chem

from chemistry.assembly import _CONNECTABLE_BOND_TYPES
from chemistry.chem_util import heavy_degree
from esmiles import (
    AttachmentContract,
    build_attachment_contract,
    contract_slot_from_label,
    ring_atoms_by_esmiles_index,
)
from esmiles import EsmilesLoadResult, atom_id_by_position
from chemistry.models import AnchorInfo
from chemistry.peel_topology import is_attachment_placeholder
from chemistry.r_groups import (
    RTagSlot,
    normalize_r_tags,
    slot_key_from_label,
)


def dummy_attachment_bond_type(
    mol: Chem.Mol, dummy_idx: int, anchor_idx: int
) -> Chem.BondType:
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
    if anchor.anchor_kind == "ring":
        # Position-variation bonds leave an aromatic/ring atom through a normal
        # exocyclic single bond.  Looking at an arbitrary in-ring template bond
        # here incorrectly turns aromatic substituents into aromatic cuts.
        return Chem.BondType.SINGLE
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
    return {
        a.template_dummy_idx: a.r_label for a in anchors if a.template_dummy_idx >= 0
    }


def template_dummy_slot_notes(anchors: list[AnchorInfo]) -> dict[int, str]:
    """Map template ``*`` atom idx → ``<a>`` label (short form) for ``atomNote`` drawing."""
    return {
        a.template_dummy_idx: a.display_label
        for a in anchors
        if a.template_dummy_idx >= 0
    }


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
    from chemistry.r_groups import placeholder_isotope_from_r_label

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


def attachment_anchor_and_dummy(
    mol: Chem.Mol, orig_anchor_idx: int
) -> tuple[int | None, int | None]:
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


def anchors_from_slots(
    template_mol: Chem.Mol, slots: list[RTagSlot]
) -> list[AnchorInfo]:
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

    # ``<r>`` is a ring-wide selector, not a fixed template atom.  It is
    # resolved only after a template→target alignment is available; choosing a
    # template exocyclic branch here loses the position-variation semantics.

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


def resolve_ring_anchor_assignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    template_to_full: dict[int, int],
    contract: AttachmentContract | None,
    base_anchors: list[AnchorInfo],
    *,
    max_assignments: int | None = 256,
    messages: list[str] | None = None,
) -> list[list[AnchorInfo]]:
    """Resolve ring-wide ``<r>`` selectors for one concrete alignment.

    The returned alternatives contain ordinary, fixed ``AnchorInfo`` objects,
    so the existing component peel pipeline remains unchanged.  The first
    implementation deliberately resolves one site per ``<r>`` occurrence;
    multiple tags are expanded as a Cartesian product with no reused site.

    ``max_assignments`` caps that product (default 256, matching the methylene
    resolver).  Pass ``None`` for unlimited enumeration.  Truncation keeps the
    earliest combinations: lower counts first, then sites in ring-atom order,
    with visible outside branches preferred over H-cap sites.
    """
    if contract is None or not contract.ring_substituents:
        return [list(base_anchors)]

    core_full = set(template_to_full.values())
    alternatives: list[list[AnchorInfo]] = [list(base_anchors)]
    seen_by_pair: dict[str, int] = {}
    used_template_base = {
        idx for anchor in base_anchors for idx in template_anchor_indices(anchor)
    }
    truncated = False

    for tag in contract.ring_substituents:
        ring_atoms = ring_atoms_by_esmiles_index(template_mol, tag.ring_index)
        # MCS candidates that do not contain the complete selected ring cannot
        # establish the position-variation semantics.
        if not ring_atoms or not ring_atoms.issubset(template_to_full):
            return []

        pk = tag.pair_key
        first_occurrence = seen_by_pair.get(pk, 0) + 1

        outside_sites: list[int] = []
        hydrogen_sites: list[int] = []
        for template_idx in sorted(ring_atoms):
            if template_idx in used_template_base:
                continue
            full_idx = template_to_full[template_idx]
            target_atom = target_mol.GetAtomWithIdx(full_idx)
            has_implicit_h = target_atom.GetTotalNumHs(includeNeighbors=True) > 0
            if outside_anchor_neighbors(target_mol, full_idx, core_full):
                outside_sites.append(template_idx)
            elif has_implicit_h:
                hydrogen_sites.append(template_idx)
        # A visible non-core branch is stronger evidence than an H-cap. Only
        # fall back to H-bearing positions when the selected ring has no
        # peelable heavy/open branch at all.
        sites = outside_sites or hydrogen_sites

        min_count, max_count = tag.count_range
        count_choices = range(min_count, min(max_count, len(sites)) + 1)
        expanded: list[list[AnchorInfo]] = []
        hit_cap = False
        for current in alternatives:
            occupied = {
                a.template_anchor_idx for a in current if a.anchor_kind == "ring"
            }
            available = [idx for idx in sites if idx not in occupied]
            for count in count_choices:
                for selected in combinations(available, count):
                    added: list[AnchorInfo] = []
                    for offset, template_idx in enumerate(selected):
                        occurrence = first_occurrence + offset
                        slot = contract_slot_from_label(
                            tag.base_label,
                            dummy_idx=-1,
                            smiles_position=-1,
                            occurrence=occurrence,
                        )
                        atom = template_mol.GetAtomWithIdx(template_idx)
                        added.append(
                            AnchorInfo(
                                template_dummy_idx=-1,
                                template_anchor_idx=template_idx,
                                r_label=tag.base_label,
                                internal_id=slot.internal_id,
                                display_label=slot.display_label,
                                pair_key=pk,
                                occurrence=occurrence,
                                heavy_degree=heavy_degree(template_mol, template_idx),
                                anchor_symbol=atom.GetSymbol(),
                                smiles_position=-1,
                                anchor_kind="ring",
                            )
                        )
                    expanded.append(current + added)
                    if (
                        max_assignments is not None
                        and len(expanded) >= max_assignments
                    ):
                        hit_cap = True
                        break
                if hit_cap:
                    break
            if hit_cap:
                break
        if hit_cap:
            truncated = True
        alternatives = expanded
        if not alternatives:
            return []
        seen_by_pair[pk] = first_occurrence + max_count - 1
    if truncated and messages is not None and max_assignments is not None:
        messages.append(
            f"环位点组合超过上限 {max_assignments}，已截断枚举（优先保留较低计数与靠前位点）。"
        )
    return alternatives


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
