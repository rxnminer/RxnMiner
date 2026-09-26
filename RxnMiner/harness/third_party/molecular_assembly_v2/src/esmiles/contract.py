"""Extended eSMILES extension semantics (note.md alignment).

Parses ring substituent ``<r>idx:label</r>``, virtual arc ``<v>idx:name:[from:to]</v>``,
and builds a unified attachment contract for peel / assembly.

DP child blocks ``<r>…nested…</r>`` (no ``digit:`` prefix) are skipped here;
see :func:`esmiles.loader.parse_acdv_tags`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rdkit import Chem

from chemistry.r_groups import (
    RTagSlot,
    pair_key_from_source,
    short_display_from_source,
    slot_key_from_label,
)

_CHILD_OPEN = "<r>"
_CHILD_CLOSE = "</r>"
_RING_TAG = re.compile(r"<r>(\d+):([^<]+)</r>")
_RING_REPEAT_SUFFIX = re.compile(r"^(.*)\?(\d+)(?:-(\d+))?$")
_VARC_INNER = re.compile(r"^\[(\d+):(\d+)\]$")


@dataclass(frozen=True)
class VirtualArcContract:
    arc_index: int
    name: str
    from_position: int
    to_position: int

    @property
    def pair_key(self) -> str:
        return self.name


@dataclass(frozen=True)
class RingSubstituentContract:
    ring_index: int
    label: str

    @property
    def pair_key(self) -> str:
        return pair_key_from_source(self.base_label)

    @property
    def base_label(self) -> str:
        match = _RING_REPEAT_SUFFIX.match(self.label.strip())
        return match.group(1).strip() if match else self.label.strip()

    @property
    def count_range(self) -> tuple[int, int]:
        """Allowed count for numeric ``?n`` / ``?m-n`` suffixes.

        A symbolic suffix such as ``?n`` remains a label-level feature until its
        separate repetition semantics are implemented, and therefore defaults
        to one ring substituent here.
        """
        match = _RING_REPEAT_SUFFIX.match(self.label.strip())
        if not match:
            return 1, 1
        lower = int(match.group(2))
        upper = int(match.group(3) or lower)
        return min(lower, upper), max(lower, upper)


@dataclass
class AttachmentContract:
    """Unified extension semantics for one eSMILES line."""

    atom_slots: list[RTagSlot] = field(default_factory=list)
    ring_substituents: list[RingSubstituentContract] = field(default_factory=list)
    virtual_arcs: list[VirtualArcContract] = field(default_factory=list)
    child_blocks_skipped: int = 0
    parse_warnings: list[str] = field(default_factory=list)


def _find_matching_child_close(info: str, open_pos: int) -> int:
    scan = open_pos + len(_CHILD_OPEN)
    depth = 1
    while scan < len(info) and depth > 0:
        nxt_open = info.find(_CHILD_OPEN, scan)
        nxt_close = info.find(_CHILD_CLOSE, scan)
        if nxt_close == -1:
            return -1
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            scan = nxt_open + len(_CHILD_OPEN)
            continue
        depth -= 1
        if depth == 0:
            return nxt_close
        scan = nxt_close + len(_CHILD_CLOSE)
    return -1


def parse_virtual_arc_content(content: str) -> VirtualArcContract | None:
    """Parse ``name:[from:to]`` inside ``<v>arc_idx:…</v>``."""
    colon = content.find(":")
    if colon == -1:
        return None
    name = content[:colon].strip()
    rest = content[colon + 1 :].strip()
    m = _VARC_INNER.match(rest)
    if not m or not name:
        return None
    return VirtualArcContract(
        arc_index=-1,
        name=name,
        from_position=int(m.group(1)),
        to_position=int(m.group(2)),
    )


def parse_virtual_arc_tag(position: int, value: str) -> VirtualArcContract | None:
    """Parse full ``<v>`` body after arc index."""
    arc = parse_virtual_arc_content(value)
    if arc is None:
        return None
    return VirtualArcContract(
        arc_index=position,
        name=arc.name,
        from_position=arc.from_position,
        to_position=arc.to_position,
    )


def parse_ring_substituent_tags(
    info: str,
) -> tuple[list[RingSubstituentContract], int, list[str]]:
    """Extract note.md-style ``<r>idx:label</r>``; count DP child blocks skipped."""
    tags: list[RingSubstituentContract] = []
    warnings: list[str] = []
    child_skipped = 0
    pos = 0
    while pos < len(info):
        if not info.startswith(_CHILD_OPEN, pos):
            pos += 1
            continue
        close = _find_matching_child_close(info, pos)
        if close == -1:
            warnings.append("未闭合的 <r> 块")
            break
        rm = _RING_TAG.match(info[pos : close + len(_CHILD_CLOSE)])
        if rm:
            tags.append(
                RingSubstituentContract(
                    ring_index=int(rm.group(1)),
                    label=rm.group(2).strip(),
                )
            )
        else:
            child_skipped += 1
        pos = close + len(_CHILD_CLOSE)
    return tags, child_skipped, warnings


def parse_virtual_arc_tags_from_acdv(
    v_tags: list,
) -> tuple[list[VirtualArcContract], list[str]]:
    """Convert loader ``EsmilesTag`` v-tags into structured arcs."""
    arcs: list[VirtualArcContract] = []
    warnings: list[str] = []
    for tag in v_tags:
        arc = parse_virtual_arc_tag(tag.position, tag.raw_value or tag.value)
        if arc is None:
            warnings.append(
                f"<v>{tag.position}:{tag.raw_value or tag.value}</v> 无法解析为虚拟弧"
            )
            continue
        arcs.append(arc)
    return arcs, warnings


def build_attachment_contract(
    info: str,
    atom_slots: list[RTagSlot],
    v_tags: list | None = None,
) -> AttachmentContract:
    ring_tags, child_skipped, ring_warnings = parse_ring_substituent_tags(info or "")
    arcs, arc_warnings = parse_virtual_arc_tags_from_acdv(v_tags or [])
    return AttachmentContract(
        atom_slots=list(atom_slots),
        ring_substituents=ring_tags,
        virtual_arcs=arcs,
        child_blocks_skipped=child_skipped,
        parse_warnings=ring_warnings + arc_warnings,
    )


def ring_atom_sets(mol: Chem.Mol) -> list[frozenset[int]]:
    """RDKit ring atom sets in the parser's SMILES ring order.

    ``AtomRings()`` already preserves the useful construction order for the
    eSMILES ring indices.  Sorting by atom index breaks that order for fused
    systems (notably it swaps the benzene and N-containing five-membered rings
    in the Synfacts tricyclic scaffold).
    """
    ri = mol.GetRingInfo()
    return [frozenset(r) for r in ri.AtomRings()]


def ring_atoms_by_esmiles_index(mol: Chem.Mol, ring_index: int) -> frozenset[int]:
    rings = ring_atom_sets(mol)
    if ring_index < 0 or ring_index >= len(rings):
        return frozenset()
    return rings[ring_index]


def exocyclic_attachment_points(
    mol: Chem.Mol, ring_atoms: frozenset[int]
) -> list[tuple[int, int]]:
    """Return (ring_atom_idx, exocyclic_heavy_neighbor) for each ring exit bond."""
    out: list[tuple[int, int]] = []
    for ridx in sorted(ring_atoms):
        for n in mol.GetAtomWithIdx(ridx).GetNeighbors():
            j = n.GetIdx()
            if n.GetAtomicNum() > 1 and j not in ring_atoms:
                out.append((ridx, j))
    return out


def pick_ring_substituent_anchor(
    mol: Chem.Mol,
    ring_index: int,
    *,
    exclude_positions: set[int] | None = None,
) -> tuple[int, int] | None:
    """Best (ring_anchor, first_sidechain_atom) for a ring substituent label.

    Picks the exocyclic branch with the largest heavy-atom subtree (ties: lowest idx).
    """
    exclude_positions = exclude_positions or set()
    ring_atoms = ring_atoms_by_esmiles_index(mol, ring_index)
    if not ring_atoms:
        return None

    def subtree_size(start: int, blocked: set[int]) -> int:
        seen: set[int] = set()
        stack = [start]
        while stack:
            i = stack.pop()
            if i in blocked or i in seen:
                continue
            a = mol.GetAtomWithIdx(i)
            if a.GetAtomicNum() <= 1:
                continue
            seen.add(i)
            for n in a.GetNeighbors():
                if n.GetAtomicNum() > 1:
                    stack.append(n.GetIdx())
        return len(seen)

    blocked = set(ring_atoms)
    best: tuple[int, int, int] | None = None
    for ring_idx, side_idx in exocyclic_attachment_points(mol, ring_atoms):
        if ring_idx in exclude_positions or side_idx in exclude_positions:
            continue
        size = subtree_size(side_idx, blocked)
        if best is None or size > best[2] or (size == best[2] and side_idx < best[1]):
            best = (ring_idx, side_idx, size)
    if best is None:
        return None
    return best[0], best[1]


def contract_slot_from_label(
    label: str,
    *,
    dummy_idx: int,
    smiles_position: int,
    occurrence: int = 1,
) -> RTagSlot:
    pk = pair_key_from_source(label)
    sk = slot_key_from_label(label, occurrence)
    return RTagSlot(
        dummy_idx=dummy_idx,
        source_label=label,
        internal_id=sk,
        display_label=short_display_from_source(label),
        pair_key=pk,
        occurrence=occurrence,
        smiles_position=smiles_position,
    )


# --- Virtual arc alignment validation (post MCS / substructure) ---


@dataclass(frozen=True)
class VirtualArcCheckResult:
    arc_index: int
    name: str
    from_position: int
    to_position: int
    template_from: int
    template_to: int
    full_from: int
    full_to: int
    satisfied: bool
    mode: str  # direct_bond | via_substituent | via_scaffold | unmapped | disconnected
    note: str = ""


def _atom_idx_at_position(atom_by_position: list[int], position: int) -> int:
    if position < 0 or position >= len(atom_by_position):
        return -1
    return atom_by_position[position]


def arc_closure_template_atoms(mol: Chem.Mol, from_idx: int, to_idx: int) -> set[int]:
    """Template atoms in the virtual-arc closure (path + rings touched by that path)."""
    if from_idx < 0 or to_idx < 0:
        return set()

    prev: dict[int, int | None] = {from_idx: None}
    stack = [from_idx]
    found = from_idx == to_idx
    while stack and not found:
        u = stack.pop()
        for n in mol.GetAtomWithIdx(u).GetNeighbors():
            if n.GetAtomicNum() <= 1:
                continue
            v = n.GetIdx()
            if v in prev:
                continue
            prev[v] = u
            if v == to_idx:
                found = True
                break
            stack.append(v)

    zone: set[int] = set()
    if found:
        cur: int | None = to_idx
        while cur is not None:
            zone.add(cur)
            cur = prev.get(cur)
    else:
        zone = {from_idx, to_idx}

    try:
        rings = [frozenset(r) for r in mol.GetRingInfo().AtomRings()]
    except Exception:
        rings = []

    for ring in rings:
        if ring & zone == ring:
            continue
        if from_idx in ring and to_idx in ring:
            zone |= set(ring)

    expanded = True
    while expanded:
        expanded = False
        for ring in rings:
            if ring & zone and not ring <= zone:
                zone |= set(ring)
                expanded = True

    return zone


def virtual_arc_zone_template_atoms(
    mol: Chem.Mol,
    virtual_arcs: list[VirtualArcContract],
    atom_by_position: list[int],
) -> set[int]:
    """Union of template atom indices inside all declared virtual-arc closure regions."""
    zone: set[int] = set()
    for arc in virtual_arcs:
        fi = _atom_idx_at_position(atom_by_position, arc.from_position)
        ti = _atom_idx_at_position(atom_by_position, arc.to_position)
        zone |= arc_closure_template_atoms(mol, fi, ti)
    return zone


def path_via_non_core(
    target_mol: Chem.Mol,
    start: int,
    end: int,
    core_full: set[int],
) -> bool:
    """BFS: path start→end using at least one heavy atom outside ``core_full``."""
    if start == end:
        return False
    stack: list[tuple[int, bool]] = [(start, False)]
    seen: set[tuple[int, bool]] = {(start, False)}
    while stack:
        idx, left_core = stack.pop()
        if idx == end and left_core:
            return True
        for n in target_mol.GetAtomWithIdx(idx).GetNeighbors():
            if n.GetAtomicNum() <= 1:
                continue
            j = n.GetIdx()
            nc = left_core or (j not in core_full)
            st = (j, nc)
            if st in seen:
                continue
            seen.add(st)
            stack.append(st)
    return False


def path_within_core(
    target_mol: Chem.Mol,
    start: int,
    end: int,
    core_full: set[int],
) -> bool:
    """BFS: path start→end using only heavy atoms in ``core_full``."""
    if start == end:
        return False
    if start not in core_full or end not in core_full:
        return False
    stack = [start]
    seen = {start}
    while stack:
        idx = stack.pop()
        if idx == end:
            return True
        for n in target_mol.GetAtomWithIdx(idx).GetNeighbors():
            if n.GetAtomicNum() <= 1:
                continue
            j = n.GetIdx()
            if j not in core_full or j in seen:
                continue
            seen.add(j)
            stack.append(j)
    return False


def virtual_arc_satisfied(
    target_mol: Chem.Mol,
    full_from: int,
    full_to: int,
    core_full: set[int],
) -> tuple[bool, str, str]:
    """Check one mapped virtual arc on the product.

    Satisfied if:
    - direct bond between endpoints, or
    - path via substituent (non-core) matter, or
    - both endpoints on scaffold and connected within scaffold only.
    """
    if full_from < 0 or full_to < 0:
        return False, "unmapped", "端点未映射到产物"
    if full_from == full_to:
        return False, "disconnected", "弧两端点为同一原子"

    bond = target_mol.GetBondBetweenAtoms(full_from, full_to)
    if bond is not None:
        return True, "direct_bond", "产物中两端点直接成键"

    if path_via_non_core(target_mol, full_from, full_to, core_full):
        return True, "via_substituent", "经骨架外原子连通（开弧填充）"

    if (
        full_from in core_full
        and full_to in core_full
        and path_within_core(target_mol, full_from, full_to, core_full)
    ):
        return True, "via_scaffold", "两端点均在骨架内且经骨架连通"

    return False, "disconnected", "两端点无法经直接键、取代基或骨架内路径连通"


def validate_virtual_arcs_for_alignment(
    target_mol: Chem.Mol,
    template_to_full: dict[int, int],
    virtual_arcs: list[VirtualArcContract],
    atom_by_position: list[int],
) -> tuple[bool, list[VirtualArcCheckResult]]:
    """Post-alignment check: virtual arcs are not part of MCS/substructure query."""
    if not virtual_arcs:
        return True, []

    core_full = set(template_to_full.values())
    results: list[VirtualArcCheckResult] = []
    all_ok = True

    for arc in virtual_arcs:
        t_from = _atom_idx_at_position(atom_by_position, arc.from_position)
        t_to = _atom_idx_at_position(atom_by_position, arc.to_position)
        f_from = template_to_full.get(t_from, -1) if t_from >= 0 else -1
        f_to = template_to_full.get(t_to, -1) if t_to >= 0 else -1

        if t_from < 0 or t_to < 0:
            ok, mode, note = False, "unmapped", "模板 SMILES 位置无效"
        elif f_from < 0 or f_to < 0:
            ok, mode, note = False, "unmapped", "弧端点未落入当前骨架映射"
        else:
            ok, mode, note = virtual_arc_satisfied(target_mol, f_from, f_to, core_full)

        if not ok:
            all_ok = False
        results.append(
            VirtualArcCheckResult(
                arc_index=arc.arc_index,
                name=arc.name,
                from_position=arc.from_position,
                to_position=arc.to_position,
                template_from=t_from,
                template_to=t_to,
                full_from=f_from,
                full_to=f_to,
                satisfied=ok,
                mode=mode,
                note=note,
            )
        )

    return all_ok, results
