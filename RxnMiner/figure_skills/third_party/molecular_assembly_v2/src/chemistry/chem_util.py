"""Small RDKit helpers for decompose."""
from __future__ import annotations

from typing import Any

from rdkit import Chem
from rdkit.Chem import inchi

from chemistry.models import BOND_TYPE_ZH


def heavy_atom_count(mol: Chem.Mol) -> int:
    return sum(1 for a in mol.GetAtoms() if a.GetAtomicNum() > 1)


def bond_label(bt: Chem.BondType) -> str:
    return BOND_TYPE_ZH.get(bt, str(bt))


def heavy_degree(mol: Chem.Mol, idx: int) -> int:
    atom = mol.GetAtomWithIdx(idx)
    return sum(1 for n in atom.GetNeighbors() if n.GetAtomicNum() > 1)


def mol_inchi_key(mol: Chem.Mol | None, *, stereo: bool = True) -> str:
    if mol is None:
        return ""
    try:
        if stereo:
            return Chem.MolToInchiKey(mol)
        return inchi.MolToInchiKey(mol, options="/SNon")
    except Exception:
        return ""


def compare_mols_equivalence(mol_expected: Chem.Mol, mol_got: Chem.Mol) -> tuple[bool, str]:
    """Layered match: full stereo InChI → canonical SMILES → connectivity InChI (/SNon)."""
    key_e = mol_inchi_key(mol_expected, stereo=True)
    key_g = mol_inchi_key(mol_got, stereo=True)
    if key_e and key_g and key_e == key_g:
        return True, f"InChIKey 一致（{key_e}）"

    can_e = Chem.MolToSmiles(mol_expected, canonical=True, isomericSmiles=True)
    can_g = Chem.MolToSmiles(mol_got, canonical=True, isomericSmiles=True)
    if can_e and can_g and can_e == can_g:
        return True, "canonical isomeric SMILES 一致"

    conn_e = mol_inchi_key(mol_expected, stereo=False)
    conn_g = mol_inchi_key(mol_got, stereo=False)
    if conn_e and conn_g and conn_e == conn_g:
        return True, f"连接性一致（InChI /SNon {conn_e}）；双键 / 标注立体与 JSON 前后不一致"

    short_e = can_e if len(can_e) <= 50 else can_e[:47] + "..."
    short_g = can_g if len(can_g) <= 50 else can_g[:47] + "..."
    return False, f"不一致：标注 {short_e} ≠ 组装 {short_g}"
