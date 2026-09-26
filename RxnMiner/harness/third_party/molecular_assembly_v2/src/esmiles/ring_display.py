"""Display helpers for eSMILES ``<r>`` ring substituents (position variation).

Maps ``<r>ring_idx:label</r>`` onto RDKit V3000-style position-variation bonds
(``_MolFileBondEndPts`` / ``_MolFileBondAttach``) so MolDraw2D draws a bond from
the ring center with candidate atoms highlighted — Ketcher-like uncertain ring
attachment.

Topology note: connecting the label dummy directly to a ring atom makes
MolDraw2D stroke that atom→label bond (often through the ring from a fusion
atom). We instead insert a hidden center dummy at the ring centroid and bond
``center — label``, with ENDPTS listing the ring atoms.
"""

from __future__ import annotations

import math

from rdkit import Chem
from rdkit.Geometry import Point3D

from .contract import RingSubstituentContract, ring_atoms_by_esmiles_index
from .loader import strip_sgroup_suffix

# Comma-separated ring atom indices (for post-layout placement).
_RING_VAR_ATOMS_PROP = "_ringVarAtoms"
# ``center`` = hidden ring-centroid dummy; ``label`` = R-group dummy outside.
_RING_VAR_ROLE_PROP = "_ringVarRole"


def attach_ring_position_variations(
    mol: Chem.Mol,
    ring_substituents: list[RingSubstituentContract],
) -> tuple[Chem.Mol, dict[int, str]]:
    """Append position-variation dummies for each simple ``<r>`` label.

    Returns ``(mol, {label_dummy_atom_idx: label})``. Nested DP ``<r>`` child
    blocks are not passed here (``parse_ring_substituent_tags`` already skips them).
    """
    if mol is None or not ring_substituents:
        return mol, {}

    rw = Chem.RWMol(mol)
    Chem.GetSymmSSSR(rw)
    labels: dict[int, str] = {}

    for tag in ring_substituents:
        label = strip_sgroup_suffix((tag.label or "").strip())
        if not label:
            continue
        ring_atoms = sorted(ring_atoms_by_esmiles_index(rw, tag.ring_index))
        if not ring_atoms:
            continue

        ring_csv = ",".join(str(i) for i in ring_atoms)
        center_idx = rw.AddAtom(Chem.Atom(0))
        label_idx = rw.AddAtom(Chem.Atom(0))
        rw.AddBond(center_idx, label_idx, Chem.BondType.SINGLE)
        bond = rw.GetBondBetweenAtoms(center_idx, label_idx)
        if bond is None:
            continue
        # V3000 ENDPTS: (n idx1 idx2 …) with 1-based atom indices.
        ones = [i + 1 for i in ring_atoms]
        bond.SetProp(
            "_MolFileBondEndPts",
            f"({len(ones)} {' '.join(str(i) for i in ones)})",
        )
        bond.SetProp("_MolFileBondAttach", "ANY")

        center = rw.GetAtomWithIdx(center_idx)
        center.SetProp(_RING_VAR_ATOMS_PROP, ring_csv)
        center.SetProp(_RING_VAR_ROLE_PROP, "center")
        # Suppress “*” at the ring center.
        center.SetProp("atomLabel", "")

        label_atom = rw.GetAtomWithIdx(label_idx)
        label_atom.SetProp(_RING_VAR_ATOMS_PROP, ring_csv)
        label_atom.SetProp(_RING_VAR_ROLE_PROP, "label")
        labels[label_idx] = label

    return rw.GetMol(), labels


def place_position_variation_dummies(mol: Chem.Mol) -> None:
    """Place center dummies at ring centroids and label dummies outside."""
    if mol is None or mol.GetNumConformers() == 0:
        return
    conf = mol.GetConformer()
    n = mol.GetNumAtoms()

    # Molecule centroid excluding variation dummies (stable outward direction).
    base_idxs = [
        i for i in range(n) if not mol.GetAtomWithIdx(i).HasProp(_RING_VAR_ROLE_PROP)
    ]
    if base_idxs:
        mx = sum(conf.GetAtomPosition(i).x for i in base_idxs) / len(base_idxs)
        my = sum(conf.GetAtomPosition(i).y for i in base_idxs) / len(base_idxs)
    else:
        mx, my = 0.0, 0.0

    for atom in mol.GetAtoms():
        if not atom.HasProp(_RING_VAR_ATOMS_PROP):
            continue
        raw = atom.GetProp(_RING_VAR_ATOMS_PROP)
        ring_atoms = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        ring_atoms = [i for i in ring_atoms if 0 <= i < n]
        if not ring_atoms:
            continue

        cx = sum(conf.GetAtomPosition(i).x for i in ring_atoms) / len(ring_atoms)
        cy = sum(conf.GetAtomPosition(i).y for i in ring_atoms) / len(ring_atoms)
        role = (
            atom.GetProp(_RING_VAR_ROLE_PROP)
            if atom.HasProp(_RING_VAR_ROLE_PROP)
            else "label"
        )

        if role == "center":
            conf.SetAtomPosition(atom.GetIdx(), Point3D(cx, cy, 0.0))
            continue

        dx, dy = cx - mx, cy - my
        norm = math.hypot(dx, dy)
        if norm < 1e-8:
            # A standalone ring has the same centroid as the whole molecule.
            # Use one ring vertex as a deterministic outward direction instead
            # of leaving the position-variation label on top of the ring center.
            vertex = conf.GetAtomPosition(ring_atoms[0])
            dx, dy = vertex.x - cx, vertex.y - cy
            norm = math.hypot(dx, dy)
        if norm < 1e-8:
            dx, dy, norm = -1.0, 0.0, 1.0
        scale = 1.7
        conf.SetAtomPosition(
            atom.GetIdx(),
            Point3D(cx + dx / norm * scale, cy + dy / norm * scale, 0.0),
        )
