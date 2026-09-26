"""Scaffold embedding enumeration and bond-order validation."""

from __future__ import annotations

from typing import Any

from rdkit import Chem
from rdkit.Chem import rdFMCS

from chemistry.chem_util import bond_label
from chemistry.models import BondMismatch
from chemistry.peel_topology import is_attachment_placeholder


def scaffold_heavy_mol(scaffold_smiles: str) -> Chem.Mol | None:
    mol = Chem.MolFromSmiles(scaffold_smiles)
    if mol is None:
        return None
    query, _ = template_heavy_query_mol(mol)
    return query


def template_heavy_query_mol(
    template_mol: Chem.Mol,
) -> tuple[Chem.Mol | None, dict[int, int]]:
    """Heavy-atom induced query; query_idx -> template_atom_idx.

    Build a SMARTS query rather than sanitizing the detached core as a normal
    molecule.  Removing a substituent from aromatic ``n(*)`` temporarily leaves
    an open-valence aromatic atom; that is a valid substructure query but is not
    necessarily a sanitizable standalone molecule.
    """
    # Exclude * / Ts placeholders (Ts is Z=117 and would otherwise enter the query).
    heavy = [
        i
        for i, a in enumerate(template_mol.GetAtoms())
        if a.GetAtomicNum() > 1 and not is_attachment_placeholder(a)
    ]
    if not heavy:
        return None, {}
    h2n = {old: new for new, old in enumerate(heavy)}
    n2t = {new: old for old, new in h2n.items()}
    em = Chem.EditableMol(Chem.RWMol())
    for old in heavy:
        source_atom = template_mol.GetAtomWithIdx(old)
        atom = Chem.Atom(source_atom.GetAtomicNum())
        atom.SetIsAromatic(source_atom.GetIsAromatic())
        atom.SetFormalCharge(source_atom.GetFormalCharge())
        em.AddAtom(atom)
    for bond in template_mol.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if i in h2n and j in h2n:
            em.AddBond(h2n[i], h2n[j], bond.GetBondType())
    try:
        m = em.GetMol()
        m.UpdatePropertyCache(strict=False)
    except Exception:
        return None, {}
    return m, n2t


def bond_types_match(bt_template: Chem.BondType, bt_full: Chem.BondType) -> bool:
    if bt_template == bt_full:
        return True
    aromatic = {Chem.BondType.AROMATIC, Chem.BondType.SINGLE}
    if bt_template in aromatic and bt_full in aromatic:
        return True
    return False


def bond_types_match_arc_zone(
    bt_template: Chem.BondType, bt_full: Chem.BondType
) -> bool:
    """Virtual-arc closure: quinone DOUBLE vs fused-ring AROMATIC is equivalent."""
    if bond_types_match(bt_template, bt_full):
        return True
    pi_like = {
        Chem.BondType.AROMATIC,
        Chem.BondType.DOUBLE,
        Chem.BondType.SINGLE,
    }
    return bt_template in pi_like and bt_full in pi_like


def check_mapped_bond_orders(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    template_to_full: dict[int, int],
    *,
    arc_zone_atoms: set[int] | None = None,
) -> tuple[bool, list[BondMismatch]]:
    mismatches: list[BondMismatch] = []
    zone = arc_zone_atoms or set()
    for bond in template_mol.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if i not in template_to_full or j not in template_to_full:
            continue
        ti, tj = template_to_full[i], template_to_full[j]
        tb = target_mol.GetBondBetweenAtoms(ti, tj)
        if tb is None:
            mismatches.append(
                BondMismatch(
                    template_i=i,
                    template_j=j,
                    full_i=ti,
                    full_j=tj,
                    template_bond=bond_label(bond.GetBondType()),
                    full_bond="（无键）",
                )
            )
            continue
        in_arc = zone and i in zone and j in zone
        matcher = bond_types_match_arc_zone if in_arc else bond_types_match
        if not matcher(bond.GetBondType(), tb.GetBondType()):
            mismatches.append(
                BondMismatch(
                    template_i=i,
                    template_j=j,
                    full_i=ti,
                    full_j=tj,
                    template_bond=bond_label(bond.GetBondType()),
                    full_bond=bond_label(tb.GetBondType()),
                )
            )
    return len(mismatches) == 0, mismatches


def dedupe_template_to_full_maps(maps: list[dict[int, int]]) -> list[dict[int, int]]:
    seen: set[frozenset[tuple[int, int]]] = set()
    out: list[dict[int, int]] = []
    for t2f in maps:
        key = frozenset(t2f.items())
        if key in seen:
            continue
        seen.add(key)
        out.append(t2f)
    return out


def enumerate_substructure_alignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    *,
    uniquify: bool = False,
) -> list[dict[int, int]]:
    """B: template heavy-atom subgraph embedded in target (keeps *-disconnect topology)."""
    query, query_to_template = template_heavy_query_mol(template_mol)
    if query is None:
        return []
    f_refs = target_mol.GetSubstructMatches(query, uniquify=uniquify)
    if not f_refs:
        return []
    raw = [
        {query_to_template[k]: int(full_idx) for k, full_idx in enumerate(f_ref)}
        for f_ref in f_refs
        if len(f_ref) == query.GetNumAtoms()
    ]
    return dedupe_template_to_full_maps(raw)


def enumerate_mcs_alignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    *,
    bond_compare: Any,
    uniquify: bool = False,
) -> tuple[list[dict[int, int]], int]:
    """A: MCS embedding; returns maps and MCS atom count."""
    query, query_to_template = template_heavy_query_mol(template_mol)
    if query is None:
        return [], 0
    try:
        res = rdFMCS.FindMCS(
            [query, target_mol],
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
    q_hits = query.GetSubstructMatches(mcs, uniquify=uniquify)
    t_hits = target_mol.GetSubstructMatches(mcs, uniquify=uniquify)
    if not q_hits or not t_hits:
        return [], mcs_atoms

    raw: list[dict[int, int]] = []
    for q_match in q_hits:
        for t_match in t_hits:
            if len(q_match) != len(t_match):
                continue
            raw.append(
                {
                    query_to_template[int(q_match[k])]: int(t_match[k])
                    for k in range(len(q_match))
                }
            )
    return dedupe_template_to_full_maps(raw), mcs_atoms


def enumerate_topology_alignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
) -> list[dict[int, int]]:
    """Legacy alias: MCS CompareAny, uniquify=False."""
    maps, _ = enumerate_mcs_alignments(
        template_mol,
        target_mol,
        bond_compare=rdFMCS.BondCompare.CompareAny,
        uniquify=False,
    )
    return maps


def collect_all_alignments(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
) -> list[tuple[str, dict[int, int], dict[str, Any]]]:
    """Run B + A (order + topology); return (source_label, t2f, meta)."""
    out: list[tuple[str, dict[int, int], dict[str, Any]]] = []

    b_maps = enumerate_substructure_alignments(template_mol, target_mol, uniquify=False)
    for t2f in b_maps:
        out.append(("B·子结构", t2f, {"query_atoms": len(t2f)}))

    order_maps, mcs_n = enumerate_mcs_alignments(
        template_mol,
        target_mol,
        bond_compare=rdFMCS.BondCompare.CompareOrder,
        uniquify=False,
    )
    for t2f in order_maps:
        out.append(("A·MCS键级", t2f, {"mcs_atoms": mcs_n}))

    any_maps, mcs_n_any = enumerate_mcs_alignments(
        template_mol,
        target_mol,
        bond_compare=rdFMCS.BondCompare.CompareAny,
        uniquify=False,
    )
    for t2f in any_maps:
        out.append(("A·MCS拓扑", t2f, {"mcs_atoms": mcs_n_any}))

    return out


def template_heavy_embeds(mol: Chem.Mol, template_mol: Chem.Mol) -> bool:
    query, _ = template_heavy_query_mol(template_mol)
    if query is None:
        return True
    return bool(mol.GetSubstructMatches(query, uniquify=True))
