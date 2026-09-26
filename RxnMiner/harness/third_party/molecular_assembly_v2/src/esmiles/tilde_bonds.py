"""Recover RDKit ``~`` (unspecified) bonds dropped at ring closures (rdkit/rdkit#3055).

RDKit parses inter-atom ``~`` as ``BondType.UNSPECIFIED`` but ring closures written as
``~N`` (and openings ``~N`` on the same atom) are incorrectly stored as ``SINGLE``.
We tokenize the SMILES body in parse order and patch only the bonds that should be
``UNSPECIFIED`` according to the string.

Display note: MolDraw2D mis-draws ``UNSPECIFIED`` bonds that touch aromatic atoms
(nested dashed hexagon for benzene; solid line for aromatic-side ring closures).
``prepare_tilde_draw_mol`` + ``overlay_tilde_bonds`` work around that for cards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import io
import math

from PIL import Image, ImageDraw
from rdkit import Chem

_BOND_SYMBOLS = frozenset("-=#$:/\\.~")
_ORGANIC = frozenset("BCNOPSFI*")
_AROMATIC = frozenset("bcnops")
_HALOGENS = ("Cl", "Br")

# Mol property: "a,b;c,d" parse-order pairs that should depict as unspecified.
TILDE_PAIRS_PROP = "__tildeBondPairs"
# Mol property: pairs demoted to SINGLE for DrawMolecule; dashed on the PNG after.
TILDE_OVERLAY_PROP = "__tildeOverlayPairs"

# Match RDKit MolDrawOptions.queryColour (#7F7F7F) for unspecified / query bonds.
_QUERY_GREY_RGB = (0x7F, 0x7F, 0x7F)


class _BondKind(Enum):
    DEFAULT = "default"
    SINGLE = "single"
    UNSPEC = "unspec"


@dataclass
class _ParseState:
    smiles: str
    pos: int = 0
    atom_index: int = 0
    prev_atom: int | None = None
    pending_bond: _BondKind = _BondKind.DEFAULT
    rings: dict[int, tuple[int, _BondKind]] = field(default_factory=dict)
    unspecified: set[tuple[int, int]] = field(default_factory=set)

    def _peek(self) -> str:
        return self.smiles[self.pos] if self.pos < len(self.smiles) else ""

    def _advance(self, n: int = 1) -> None:
        self.pos += n

    def _add_unspec(self, a: int, b: int) -> None:
        self.unspecified.add((min(a, b), max(a, b)))

    def _bond_from_symbol(self, ch: str) -> _BondKind:
        if ch == "~":
            return _BondKind.UNSPEC
        return _BondKind.SINGLE

    def _read_optional_bond(self) -> _BondKind | None:
        ch = self._peek()
        if ch not in _BOND_SYMBOLS:
            return None
        self._advance()
        return self._bond_from_symbol(ch)

    def _read_ring_number(self) -> int | None:
        ch = self._peek()
        if ch == "%":
            if self.pos + 3 > len(self.smiles):
                return None
            digits = self.smiles[self.pos + 1 : self.pos + 3]
            if not digits.isdigit():
                return None
            self._advance(3)
            return int(digits)
        if ch.isdigit():
            self._advance()
            return int(ch)
        return None

    def _read_bracket_atom(self) -> bool:
        if self._peek() != "[":
            return False
        start = self.pos
        depth = 0
        while self.pos < len(self.smiles):
            ch = self.smiles[self.pos]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    self._advance()
                    return True
            self._advance()
        self.pos = start
        return False

    def _read_atom(self) -> bool:
        ch = self._peek()
        if not ch:
            return False
        if ch == "[":
            return self._read_bracket_atom()
        if ch in _AROMATIC or ch in _ORGANIC:
            self._advance()
            return True
        if self.smiles.startswith("Cl", self.pos):
            self._advance(2)
            return True
        if self.smiles.startswith("Br", self.pos):
            self._advance(2)
            return True
        return False

    def _connect_pending(self, cur: int) -> None:
        if self.prev_atom is None:
            return
        if self.pending_bond == _BondKind.UNSPEC:
            self._add_unspec(self.prev_atom, cur)
        self.pending_bond = _BondKind.DEFAULT

    def _handle_ring(self, cur: int, bond_prefix: _BondKind | None) -> None:
        ring_num = self._read_ring_number()
        if ring_num is None:
            return
        if ring_num in self.rings:
            open_atom, open_bond = self.rings.pop(ring_num)
            close_bond = bond_prefix if bond_prefix is not None else open_bond
            if close_bond == _BondKind.UNSPEC:
                self._add_unspec(cur, open_atom)
        else:
            stored = bond_prefix if bond_prefix is not None else _BondKind.DEFAULT
            self.rings[ring_num] = (cur, stored)

    def _parse_chain(self) -> None:
        while self.pos < len(self.smiles):
            ch = self._peek()
            if not ch:
                break
            if ch == ")":
                return
            if ch == "(":
                self._advance()
                self._parse_chain()
                if self._peek() == ")":
                    self._advance()
                continue
            if ch == ".":
                self._advance()
                self.prev_atom = None
                self.pending_bond = _BondKind.DEFAULT
                continue
            if not self._read_atom():
                self._advance()
                continue

            cur = self.atom_index
            self.atom_index += 1
            self._connect_pending(cur)

            while True:
                bond_prefix = self._read_optional_bond()
                if bond_prefix is None and not self._peek().isdigit() and self._peek() != "%":
                    break
                if bond_prefix is None and self._peek() not in "0123456789%":
                    break
                if self._peek() in "0123456789%":
                    self._handle_ring(cur, bond_prefix)
                elif bond_prefix is not None:
                    self.pending_bond = bond_prefix
                    break
                else:
                    break

            bond = self._read_optional_bond()
            if bond is not None:
                self.pending_bond = bond
            self.prev_atom = cur


def unspecified_bond_pairs(smiles: str) -> set[tuple[int, int]]:
    """Atom-index pairs (SMILES parse order) that should be ``BondType.UNSPECIFIED``."""
    if "~" not in smiles:
        return set()
    state = _ParseState(smiles=smiles.strip())
    state._parse_chain()
    return state.unspecified


def _set_unspecified_bond(rw: Chem.RWMol, i: int, j: int) -> bool:
    bond = rw.GetBondBetweenAtoms(i, j)
    if bond is None:
        return False
    if bond.GetBondType() == Chem.BondType.UNSPECIFIED:
        return False
    bond.SetBondType(Chem.BondType.UNSPECIFIED)
    return True


def encode_bond_pairs(pairs: set[tuple[int, int]]) -> str:
    return ";".join(f"{a},{b}" for a, b in sorted(pairs))


def decode_bond_pairs(raw: str) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "," not in part:
            continue
        left, right = part.split(",", 1)
        try:
            a, b = int(left), int(right)
        except ValueError:
            continue
        out.add((min(a, b), max(a, b)))
    return out


def get_tilde_pairs(mol: Chem.Mol, smiles: str | None = None) -> set[tuple[int, int]]:
    """Resolve ``~`` pairs from a mol property and/or SMILES body."""
    if mol is not None and mol.HasProp(TILDE_PAIRS_PROP):
        return decode_bond_pairs(mol.GetProp(TILDE_PAIRS_PROP))
    if smiles and "~" in smiles:
        return unspecified_bond_pairs(smiles)
    return set()


def aromatic_touching_pairs(
    mol: Chem.Mol, pairs: set[tuple[int, int]]
) -> set[tuple[int, int]]:
    """``~`` bonds with at least one aromatic endpoint (MolDraw2D fails on these)."""
    n = mol.GetNumAtoms()
    out: set[tuple[int, int]] = set()
    for a, b in pairs:
        if a >= n or b >= n:
            continue
        if mol.GetBondBetweenAtoms(a, b) is None:
            continue
        if mol.GetAtomWithIdx(a).GetIsAromatic() or mol.GetAtomWithIdx(b).GetIsAromatic():
            out.add((a, b))
    return out


def _has_aromatic_atom(mol: Chem.Mol) -> bool:
    return any(atom.GetIsAromatic() for atom in mol.GetAtoms())


def force_display_kekule(mol: Chem.Mol) -> Chem.Mol:
    """Assign explicit single/double bonds on aromatic rings for depiction.

    MolDraw2D paints a nested dashed hexagon for aromatic rings when the mol
    also carries ``UNSPECIFIED`` bonds. Explicit Kekulé bonds avoid that.
    ``Chem.Kekulize`` often fails on these markush/query graphs, so we assign
    alternating bonds from SSSR aromatic rings. Bonds that exit to a
    non-aromatic neighbor are preferred as SINGLE (tetralin-like fusion).
    """
    rw = Chem.RWMol(mol)
    Chem.GetSymmSSSR(rw)
    for ring in rw.GetRingInfo().AtomRings():
        if len(ring) < 5:
            continue
        if not all(rw.GetAtomWithIdx(i).GetIsAromatic() for i in ring):
            continue
        atoms = list(ring)
        n = len(atoms)
        atom_set = set(atoms)
        offset = 0
        for i in range(n):
            a, b = atoms[i], atoms[(i + 1) % n]
            exits_to_aliphatic = False
            for endpoint in (a, b):
                for nbr in rw.GetAtomWithIdx(endpoint).GetNeighbors():
                    j = nbr.GetIdx()
                    if j not in atom_set and not nbr.GetIsAromatic():
                        exits_to_aliphatic = True
                        break
                if exits_to_aliphatic:
                    break
            if exits_to_aliphatic:
                # Make bond i a SINGLE: (i + offset) % 2 == 1.
                offset = 0 if (i % 2 == 1) else 1
                break
        for i in range(n):
            a, b = atoms[i], atoms[(i + 1) % n]
            bond = rw.GetBondBetweenAtoms(a, b)
            if bond is None:
                continue
            bond.SetIsAromatic(False)
            bond.SetBondType(
                Chem.BondType.DOUBLE
                if ((i + offset) % 2 == 0)
                else Chem.BondType.SINGLE
            )
        for idx in atoms:
            rw.GetAtomWithIdx(idx).SetIsAromatic(False)
    return rw.GetMol()


def prepare_tilde_draw_mol(
    mol: Chem.Mol,
    pairs: set[tuple[int, int]] | None = None,
) -> Chem.Mol:
    """After coords exist: prepare a draw mol for tilde / aromatic depiction.

    Aromatic-touching ``~`` bonds are demoted to ``SINGLE`` (MolDraw2D otherwise
    draws them solid or breaks aromatic style). Remaining aromatic rings are
    Kekulé-assigned when any ``~`` is present. Dashes for demoted bonds are
    painted onto the PNG by ``paint_tilde_dashes_on_png``.
    """
    if pairs is None:
        pairs = get_tilde_pairs(mol)
    if not pairs:
        if mol.HasProp(TILDE_OVERLAY_PROP):
            mol.ClearProp(TILDE_OVERLAY_PROP)
        return mol

    # Capture aromatic endpoints before Kekulé clearing.
    overlay = aromatic_touching_pairs(mol, pairs)
    rw = Chem.RWMol(mol)
    for a, b in overlay:
        bond = rw.GetBondBetweenAtoms(a, b)
        if bond is None:
            continue
        bond.SetBondType(Chem.BondType.SINGLE)
        bond.SetIsAromatic(False)
    out = rw.GetMol()

    if _has_aromatic_atom(out):
        out = force_display_kekule(out)

    if overlay:
        out.SetProp(TILDE_OVERLAY_PROP, encode_bond_pairs(overlay))
    elif out.HasProp(TILDE_OVERLAY_PROP):
        out.ClearProp(TILDE_OVERLAY_PROP)
    if mol.HasProp(TILDE_PAIRS_PROP):
        out.SetProp(TILDE_PAIRS_PROP, mol.GetProp(TILDE_PAIRS_PROP))
    return out


def tilde_overlay_segments(
    drawer: object, mol: Chem.Mol
) -> list[tuple[float, float, float, float]]:
    """Pixel endpoints for demoted ``~`` bonds (call after ``DrawMolecule``)."""
    if not mol.HasProp(TILDE_OVERLAY_PROP):
        return []
    pairs = decode_bond_pairs(mol.GetProp(TILDE_OVERLAY_PROP))
    get_coords = getattr(drawer, "GetDrawCoords", None)
    if not pairs or get_coords is None:
        return []
    segments: list[tuple[float, float, float, float]] = []
    for a, b in sorted(pairs):
        pa = get_coords(a)
        pb = get_coords(b)
        segments.append((float(pa.x), float(pa.y), float(pb.x), float(pb.y)))
    return segments


def paint_tilde_dashes_on_png(
    png_bytes: bytes,
    segments: list[tuple[float, float, float, float]],
) -> bytes:
    """Erase solid bonds on a PNG and redraw them as gray dashes.

    MolDraw2DCairo ignores post-``DrawMolecule`` ``DrawLine`` erasures, so we
    edit the finished raster instead.
    """
    if not png_bytes or not segments:
        return png_bytes

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    # Inset so we do not wipe atom labels (*, R notes) at the endpoints.
    inset = 0.12
    erase_width = 5
    dash_width = 2
    n_segments = 16

    for x1, y1, x2, y2 in segments:
        dx, dy = x2 - x1, y2 - y1
        ax, ay = x1 + dx * inset, y1 + dy * inset
        bx, by = x1 + dx * (1.0 - inset), y1 + dy * (1.0 - inset)
        draw.line([(ax, ay), (bx, by)], fill=(255, 255, 255, 255), width=erase_width)

        seg_dx, seg_dy = bx - ax, by - ay
        length = math.hypot(seg_dx, seg_dy)
        if length < 1e-6:
            continue
        for i in range(0, n_segments, 2):
            t1 = i / n_segments
            t2 = min((i + 1) / n_segments, 1.0)
            draw.line(
                [
                    (ax + seg_dx * t1, ay + seg_dy * t1),
                    (ax + seg_dx * t2, ay + seg_dy * t2),
                ],
                fill=(*_QUERY_GREY_RGB, 255),
                width=dash_width,
            )

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def overlay_tilde_bonds(drawer: object, mol: Chem.Mol) -> None:
    """Deprecated no-op: Cairo cannot erase bonds after ``DrawMolecule``.

    Use ``tilde_overlay_segments`` + ``paint_tilde_dashes_on_png`` instead.
    """
    return


def patch_tilde_bonds(mol: Chem.Mol, smiles: str) -> Chem.Mol:
    """Patch ``~`` bonds that RDKit mis-parsed (notably ring closures ``~N``).

    Bond pairs are in RDKit atom-index space (same as SMILES parse order).
    Do not pass ``build_atom_by_position`` here — that map follows canonical
    SMILES output order used by ``<a>`` tags, not parse order.
    """
    pairs = unspecified_bond_pairs(smiles)
    if not pairs:
        return mol

    rw = Chem.RWMol(mol)
    changed = False
    n = mol.GetNumAtoms()
    for a, b in pairs:
        if a >= n or b >= n:
            continue
        if _set_unspecified_bond(rw, a, b):
            changed = True
    out = rw.GetMol() if changed else Chem.Mol(mol)
    out.SetProp(TILDE_PAIRS_PROP, encode_bond_pairs(pairs))
    return out
