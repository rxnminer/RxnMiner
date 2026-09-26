"""Minimal utils for vendored Translator (abbrev table + group merge)."""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, Tuple

from rdkit import Chem

_ABBREV_CSV = Path(__file__).with_name("abbrevs.csv")
_abbrev_smi: dict[str, str] = {}


def _load_abbrev_smi() -> None:
    if _abbrev_smi:
        return
    with _ABBREV_CSV.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = (row.get("symbol") or "").strip()
            smi = (row.get("smiles") or "").strip()
            if symbol and smi:
                _abbrev_smi[symbol] = smi


def get_abbrev_smi() -> Dict[str, str]:
    _load_abbrev_smi()
    return _abbrev_smi


def split_groups(groups: str) -> Tuple[Dict[int, Dict], Dict[int, Dict]]:
    pattern = r"<(?P<type>r|a)>(?P<content>.*?)</(?P=type)>"
    a_groups: dict[int, dict] = {}
    r_groups: dict[int, dict] = {}
    for match in re.finditer(pattern, groups):
        type_ = match.group("type")
        text = match.group("content")
        if ":" in text:
            ind, content = text.split(":", 1)
        else:
            ind, content = "", text
        if type_ == "r":
            r_groups[int(ind)] = content
        else:
            a_groups[int(ind)] = content
    return a_groups, r_groups


def get_groups_str(a_groups: Dict[int, Dict], r_groups: Dict[int, Dict] | None = None) -> str:
    groups_str = ""
    for key, value in sorted(a_groups.items(), key=lambda item: item[0]):
        groups_str += f"<a>{key}:{value['content']}</a>"
    if r_groups is not None:
        for key, value in sorted(r_groups.items(), key=lambda item: item[0]):
            groups_str += f"<r>{key}:{value['content']}</r>"
    return groups_str


def is_single_bond(bond: Chem.Bond) -> bool:
    return bond.GetBondType() == Chem.BondType.SINGLE


def get_mol(smi: str) -> Chem.Mol:
    mol = Chem.MolFromSmiles(smi)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smi}")
    return mol


def merge_group(tgt_mol: Chem.RWMol, src: str, attach_idx: int) -> None:
    """Merge an abbreviation fragment onto a dummy attachment point."""
    dummy_atom = tgt_mol.GetAtomWithIdx(attach_idx)
    if dummy_atom.GetSymbol() != "*":
        raise ValueError("Non-dummy attachment point")

    neighbors = dummy_atom.GetNeighbors()
    if len(neighbors) != 1:
        raise ValueError("Attachment point must have exactly one neighbor")
    nb_idx = neighbors[0].GetIdx()

    conn_bond = tgt_mol.GetBondBetweenAtoms(attach_idx, nb_idx)
    if conn_bond is None or conn_bond.GetBondType() != Chem.BondType.SINGLE:
        raise ValueError("Attachment point must link to single bond")

    src_mol = get_mol(src)
    idx_map: dict[int, int] = {}
    for atom in src_mol.GetAtoms():
        idx_map[atom.GetIdx()] = tgt_mol.AddAtom(atom)
    for bond in src_mol.GetBonds():
        begin_idx = bond.GetBeginAtomIdx()
        end_idx = bond.GetEndAtomIdx()
        tgt_mol.AddBond(idx_map[begin_idx], idx_map[end_idx], bond.GetBondType())

    tgt_mol.RemoveBond(attach_idx, nb_idx)
    tgt_mol.AddBond(nb_idx, idx_map[0], Chem.BondType.SINGLE)
