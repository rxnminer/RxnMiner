"""Export peeled scaffold + substituents to assembly-ready eSMILES lines."""
from __future__ import annotations

from rdkit import Chem

from anchors import peeled_dummy_to_rlabel
from esmiles_encode import encode_substituent_esmiles, position_for_atom_idx
from esmiles_loader import ESMILES_SEPARATOR, build_atom_by_position
from models import AnchorInfo, PeelResult
from peel_topology import PLACEHOLDER_Z, is_attachment_placeholder


def mol_to_assembly_line(
    scaffold_mol: Chem.Mol,
    anchors: list[AnchorInfo],
    template_to_full: dict[int, int],
) -> str:
    """Ts scaffold mol → ``SMILES<sep><a>pos:R[n]</a>`` for ``assemble_fragments``."""
    dummy_to_rlabel = peeled_dummy_to_rlabel(scaffold_mol, anchors, template_to_full)
    rw = Chem.RWMol(Chem.Mol(scaffold_mol))
    for atom in rw.GetAtoms():
        if is_attachment_placeholder(atom) and atom.GetAtomicNum() == PLACEHOLDER_Z:
            atom.SetAtomicNum(0)
    mol = rw.GetMol()
    try:
        Chem.SanitizeMol(mol)
        Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    except Exception:
        pass
    body_mol = Chem.Mol(mol)
    smiles = Chem.MolToSmiles(body_mol, isomericSmiles=True, canonical=False)
    atom_by_position = build_atom_by_position(body_mol, smiles)
    tag_parts: list[tuple[int, str]] = []
    seen_pos: set[int] = set()
    for atom in body_mol.GetAtoms():
        if atom.GetAtomicNum() != 0:
            continue
        label = (dummy_to_rlabel.get(atom.GetIdx()) or "").strip()
        if not label:
            continue
        pos = position_for_atom_idx(atom_by_position, atom.GetIdx())
        if pos < 0:
            continue
        if pos in seen_pos:
            continue
        seen_pos.add(pos)
        tag_parts.append((pos, label))
    tag_parts.sort(key=lambda x: x[0])
    if not tag_parts:
        return smiles
    tags = "".join(f"<a>{pos}:{label}</a>" for pos, label in tag_parts)
    return f"{smiles}{ESMILES_SEPARATOR}{tags}"


def fragments_from_peel(
    peel: PeelResult,
    anchors: list[AnchorInfo],
    template_to_full: dict[int, int],
) -> list[str]:
    """Assembly fragment lines: peeled scaffold first, then non-H substituents."""
    lines: list[str] = []
    if peel.scaffold_mol is not None:
        line = mol_to_assembly_line(peel.scaffold_mol, anchors, template_to_full)
        if line:
            lines.append(line)
    for sub in peel.substituents:
        smi = (sub.smiles or "").strip()
        if not smi:
            continue
        lines.append(smi)
    return lines
