"""Transfer resolved product ``<r>`` positions into reactant templates."""
from __future__ import annotations

from dataclasses import dataclass

from rdkit import Chem
from rdkit.Chem import rdFMCS

from esmiles import encode_substituent_esmiles, load_esmiles, ring_atoms_by_esmiles_index


def _label_key(label: str) -> str:
    return (label or "").strip().replace("[", "").replace("]", "").lower()


def labels_equivalent(left: str, right: str) -> bool:
    return bool(_label_key(left)) and _label_key(left) == _label_key(right)


@dataclass(frozen=True)
class RingTransferResult:
    template_line: str
    consumed_labels: tuple[str, ...] = ()
    unresolved_labels: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()


def _mapped_ring_atoms(
    product_mol: Chem.Mol,
    reactant_mol: Chem.Mol,
    product_atom_idx: int,
    reactant_ring: frozenset[int],
) -> set[int]:
    """Return reactant-ring atoms corresponding to one resolved product atom."""
    result = rdFMCS.FindMCS(
        [product_mol, reactant_mol],
        atomCompare=rdFMCS.AtomCompare.CompareElements,
        bondCompare=rdFMCS.BondCompare.CompareOrder,
        ringMatchesRingOnly=True,
        completeRingsOnly=True,
        timeout=15,
    )
    if result.canceled or result.numAtoms == 0:
        return set()
    query = Chem.MolFromSmarts(result.smartsString)
    if query is None:
        return set()
    out: set[int] = set()
    product_matches = product_mol.GetSubstructMatches(query, uniquify=True, maxMatches=256)
    reactant_matches = reactant_mol.GetSubstructMatches(query, uniquify=True, maxMatches=256)
    for product_match in product_matches:
        if product_atom_idx not in product_match:
            continue
        query_idx = product_match.index(product_atom_idx)
        for reactant_match in reactant_matches:
            candidate = reactant_match[query_idx]
            if candidate in reactant_ring:
                out.add(candidate)
    return out


def materialize_transferred_ring_slots(
    product_template: str,
    reactant_template: str,
    resolved_product_anchors: list[dict],
) -> RingTransferResult:
    """Turn uniquely transferred reactant ``<r>`` selectors into dummy ``<a>`` slots.

    Product anchors come from the selected Step0 candidate, where each ring
    selector has already resolved to a concrete product-template atom.
    """
    product_loaded = load_esmiles(product_template, prefer_indigo=False)
    reactant_loaded = load_esmiles(reactant_template, prefer_indigo=False)
    if product_loaded.mol is None or reactant_loaded.mol is None:
        return RingTransferResult(reactant_template, unresolved_labels=("<parse>",))
    contract = reactant_loaded.attachment_contract
    if contract is None or not contract.ring_substituents:
        return RingTransferResult(reactant_template)

    product_ring_anchors = [
        row for row in resolved_product_anchors if row.get("anchor_kind") == "ring"
    ]
    rw = Chem.RWMol(Chem.Mol(reactant_loaded.mol))
    labels = {slot.dummy_idx: slot.source_label for slot in reactant_loaded.anchor_slots}
    consumed: list[str] = []
    unresolved: list[str] = []
    messages: list[str] = []

    for tag in contract.ring_substituents:
        matching = [
            row for row in product_ring_anchors
            if labels_equivalent(str(row.get("r_label") or ""), tag.base_label)
        ]
        occurrence = sum(labels_equivalent(label, tag.base_label) for label in consumed) + 1
        source = next(
            (row for row in matching if int(row.get("occurrence") or 1) == occurrence),
            matching[0] if len(matching) == 1 else None,
        )
        reactant_ring = ring_atoms_by_esmiles_index(reactant_loaded.mol, tag.ring_index)
        if source is None or not reactant_ring:
            unresolved.append(tag.label)
            continue
        candidates = _mapped_ring_atoms(
            product_loaded.mol,
            reactant_loaded.mol,
            int(source["template_anchor_idx"]),
            reactant_ring,
        )
        if len(candidates) != 1:
            unresolved.append(tag.label)
            messages.append(
                f"环位点 {tag.label} 跨模板映射得到 {len(candidates)} 个候选，未自动选择。"
            )
            continue
        anchor_idx = next(iter(candidates))
        dummy_idx = rw.AddAtom(Chem.Atom(0))
        rw.AddBond(anchor_idx, dummy_idx, Chem.BondType.SINGLE)
        labels[dummy_idx] = tag.base_label
        consumed.append(tag.base_label)
        messages.append(
            f"环位点 {tag.label}: 产物模板原子 {source['template_anchor_idx']} "
            f"→ 反应物第 {tag.ring_index} 环原子 {anchor_idx}。"
        )

    mol = rw.GetMol()
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        return RingTransferResult(
            reactant_template,
            consumed_labels=tuple(consumed),
            unresolved_labels=tuple(tag.label for tag in contract.ring_substituents),
            messages=tuple(messages + ["环位点实体化后分子校验失败。"]),
        )
    encoded = encode_substituent_esmiles(mol, labels)
    if unresolved:
        encoded += "".join(
            f"<r>{tag.ring_index}:{tag.label}</r>"
            for tag in contract.ring_substituents
            if tag.label in unresolved
        )
    return RingTransferResult(
        encoded,
        consumed_labels=tuple(consumed),
        unresolved_labels=tuple(unresolved),
        messages=tuple(messages),
    )
