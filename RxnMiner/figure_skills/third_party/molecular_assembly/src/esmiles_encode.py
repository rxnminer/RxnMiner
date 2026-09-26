"""Write-back ``<sep>`` extension tags for peeled substituent fragments."""
from __future__ import annotations

from rdkit import Chem

from esmiles_loader import ESMILES_SEPARATOR, build_atom_by_position
from r_groups import placeholder_isotope_from_r_label


def position_for_atom_idx(atom_by_position: list[int], atom_idx: int) -> int:
    for pos, idx in enumerate(atom_by_position):
        if idx == atom_idx:
            return pos
    return -1


def _body_to_parsed_atom_map(body_mol: Chem.Mol, parsed: Chem.Mol) -> dict[int, int]:
    """Map atom indices on ``body_mol`` to matching indices on ``parsed`` after SMILES round-trip."""
    if body_mol.GetNumAtoms() == parsed.GetNumAtoms():
        hit = parsed.GetSubstructMatch(body_mol)
        if hit and len(hit) == body_mol.GetNumAtoms():
            return {body_i: int(parsed_i) for body_i, parsed_i in enumerate(hit)}
    hit = body_mol.GetSubstructMatch(parsed)
    if hit and len(hit) == parsed.GetNumAtoms():
        return {int(body_i): int(parsed_i) for parsed_i, body_i in enumerate(hit)}
    return {}


def remap_dummy_rlabels_after_roundtrip(
    body_mol: Chem.Mol,
    parsed: Chem.Mol,
    dummy_to_rlabel: dict[int, str],
) -> dict[int, str]:
    """``dummy_to_rlabel`` keyed on ``body_mol`` → indices on canonical ``parsed``."""
    out: dict[int, str] = {}
    if not dummy_to_rlabel:
        return out
    body_to_parsed = _body_to_parsed_atom_map(body_mol, parsed)
    for body_idx, label in dummy_to_rlabel.items():
        raw = (label or "").strip()
        if not raw:
            continue
        parsed_idx = body_to_parsed.get(body_idx)
        if parsed_idx is not None:
            out[parsed_idx] = raw
    return out


def encode_substituent_esmiles(
    mol: Chem.Mol,
    dummy_to_rlabel: dict[int, str],
) -> str:
    """``{SMILES}<sep><a>pos:label</a>`` using upstream connection labels.

    Tags use SMILES token positions from a round-trip parse so ``load_esmiles``
    and ``assemble_fragments`` see dummies at the same indices as peel export.
    """
    body_mol = Chem.Mol(mol)
    smiles = Chem.MolToSmiles(body_mol, isomericSmiles=True)
    parsed = Chem.MolFromSmiles(smiles)
    if parsed is None:
        parsed = body_mol
        remapped = dict(dummy_to_rlabel)
    else:
        smiles = Chem.MolToSmiles(parsed, isomericSmiles=True)
        remapped = remap_dummy_rlabels_after_roundtrip(body_mol, parsed, dummy_to_rlabel)
    atom_by_position = build_atom_by_position(parsed, smiles)

    iso_to_label = {
        placeholder_isotope_from_r_label(lbl): lbl
        for lbl in dummy_to_rlabel.values()
        if (lbl or "").strip()
    }
    tag_parts: list[tuple[int, str]] = []
    seen_pos: set[int] = set()
    for atom in parsed.GetAtoms():
        if atom.GetAtomicNum() != 0:
            continue
        label = ""
        iso = atom.GetIsotope()
        if iso in iso_to_label:
            label = iso_to_label[iso]
        else:
            label = (remapped.get(atom.GetIdx()) or "").strip()
        if not label:
            continue
        pos = position_for_atom_idx(atom_by_position, atom.GetIdx())
        if pos < 0 or pos in seen_pos:
            continue
        seen_pos.add(pos)
        tag_parts.append((pos, label))
    tag_parts.sort(key=lambda x: x[0])
    if not tag_parts:
        return smiles
    tags = "".join(f"<a>{pos}:{lbl}</a>" for pos, lbl in tag_parts)
    return f"{smiles}{ESMILES_SEPARATOR}{tags}"
