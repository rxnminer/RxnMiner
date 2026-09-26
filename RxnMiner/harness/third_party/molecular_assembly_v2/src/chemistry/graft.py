"""Shared substituent grafting — abbreviation expand uses the assembly join path.

Chemical op is the same as ``assemble_fragments``: replace a degree-1 ``*`` with a
concrete group (or pair two ``*`` ports). Abbreviation CSV entries are groups
without a leading ``*``; we attach ``*`` at atom 0, then call the assembly kernel.
"""
from __future__ import annotations

from rdkit import Chem

# Keep in sync with chemistry.peel_topology.PLACEHOLDER_Z (avoid import cycle via esmiles).
_PLACEHOLDER_Z = 117


def star_body_from_group_smiles(group_smiles: str) -> str:
    """Return a single-attachment fragment body: ``*`` bonded to group atom 0.

    If ``group_smiles`` already has exactly one degree-1 dummy, return it unchanged.
    """
    raw = (group_smiles or "").strip()
    if not raw:
        raise ValueError("empty group SMILES")
    mol = Chem.MolFromSmiles(raw)
    if mol is None:
        raise ValueError(f"cannot parse group SMILES: {raw[:48]}")
    stars = [a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum() == 0]
    if len(stars) == 1 and mol.GetAtomWithIdx(stars[0]).GetDegree() == 1:
        return Chem.MolToSmiles(mol, isomericSmiles=True)

    rw = Chem.RWMol(mol)
    star_idx = rw.AddAtom(Chem.Atom(0))
    rw.AddBond(0, star_idx, Chem.BondType.SINGLE)
    out = rw.GetMol()
    try:
        Chem.SanitizeMol(out)
    except Exception:
        pass
    return Chem.MolToSmiles(out, isomericSmiles=True)


def placeholders_as_star_dummies(mol: Chem.Mol) -> Chem.Mol:
    """Convert assembly Ts placeholders (Z=117) back to ``*`` (Z=0) for eSMILES encode."""
    rw = Chem.RWMol(Chem.Mol(mol))
    for atom in rw.GetAtoms():
        if atom.GetAtomicNum() == _PLACEHOLDER_Z:
            atom.SetAtomicNum(0)
    return rw.GetMol()


def remaining_dummy_rlabels(mol: Chem.Mol) -> dict[int, str]:
    """``{atom_idx: R-label}`` for unpaired attachment sites (skip internal ``__abbrev_*``)."""
    out: dict[int, str] = {}
    for atom in mol.GetAtoms():
        z = atom.GetAtomicNum()
        if z not in (0, _PLACEHOLDER_Z):
            continue
        if not atom.HasProp("_R_label"):
            continue
        label = (atom.GetProp("_R_label") or "").strip()
        if not label or label.startswith("__abbrev_"):
            continue
        out[atom.GetIdx()] = label
    return out


def assemble_labeled_fragments(
    fragment_lines: list[str],
    messages: list[str] | None = None,
    trace: list[dict[str, object]] | None = None,
) -> Chem.Mol | None:
    """Thin wrapper: assembly kernel that preserves tetrahedral tags via AtomMap transfer."""
    from chemistry.assembly import build_complete_molecule

    return build_complete_molecule(fragment_lines, messages=messages, trace=trace)
