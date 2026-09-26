"""Numeric / abstract methylene repeats: ``CH2?k`` / ``CH2?n``.

Wire form in ``<a>`` tags stays ``CH2?…``. Numeric ``k`` is expanded with a thin
adapter (remember neighbors → drop ``*`` → insert carbon chain → reconnect).
Abstract ``n`` / ranges are left in place.

Not abbreviation-table grafting and not ``Translator.refactor``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import product

from rdkit import Chem

from chemistry.graft import remaining_dummy_rlabels

from .encode import encode_substituent_esmiles
from .loader import load_esmiles

_CH2_WIRE = r"CH(?:2|\[2\])"
_CH2_NUMERIC = re.compile(rf"^{_CH2_WIRE}\?(\d+)$", re.IGNORECASE)
_CH2_RANGE = re.compile(rf"^{_CH2_WIRE}\?(\d+-\d+)$", re.IGNORECASE)
_CH2_LETTER = re.compile(rf"^{_CH2_WIRE}\?([a-z])$", re.IGNORECASE)
_CH2_DISPLAY_NUMERIC = re.compile(r"^\(CH2\)(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class MethyleneRepeatSpec:
    """One abstract ``CH2?n`` / ``CH2?m-n`` bridge in a template."""

    internal_id: str
    source_label: str
    dummy_idx: int
    endpoint_idxs: tuple[int, int]
    min_count: int
    max_count: int | None
    mode: str = "bridge"


@dataclass(frozen=True)
class MethyleneRepeatAssignment:
    internal_id: str
    count: int
    target_path_idxs: tuple[int, ...]


@dataclass
class ResolvedMethyleneTemplate:
    """A concrete template proven to embed in the supplied target."""

    esmiles: str
    mol: Chem.Mol
    assignments: tuple[MethyleneRepeatAssignment, ...]
    messages: list[str] = field(default_factory=list)


def ch2_display_form(source_label: str) -> str | None:
    """Canonical draw label ``(CH2)…`` for a methylene-repeat wire label, else None."""
    lab = (source_label or "").strip()
    m = _CH2_NUMERIC.match(lab)
    if m:
        return f"(CH2){m.group(1)}"
    m = _CH2_RANGE.match(lab)
    if m:
        return f"(CH2){m.group(1)}"
    m = _CH2_LETTER.match(lab)
    if m:
        return f"(CH2){m.group(1)}"
    if lab.upper() in {"CH2", "CH[2]"}:
        return "(CH2)1"
    return None


def parse_ch2_count(label: str) -> int | None:
    """Return ``k`` for numeric ``CH2?k`` / ``(CH2)k``; None for abstract / non-CH2."""
    lab = (label or "").strip()
    m = _CH2_NUMERIC.match(lab)
    if m:
        return int(m.group(1))
    m = _CH2_DISPLAY_NUMERIC.match(lab)
    if m:
        return int(m.group(1))
    if lab.upper() in {"CH2", "CH[2]"}:
        return 1
    return None


def is_ch2_repeat_label(label: str) -> bool:
    return ch2_display_form(label) is not None


def is_abstract_ch2_label(label: str) -> bool:
    return is_ch2_repeat_label(label) and parse_ch2_count(label) is None


def line_has_abstract_ch2(line: str) -> bool:
    from chemistry.r_groups import parse_tag_string

    from .loader import split_esmiles

    if not line or "<sep>" not in line:
        return False
    _body, extension = split_esmiles(line)
    return any(is_abstract_ch2_label(lbl) for _, lbl in parse_tag_string(extension))


def _add_carbon_chain(rw: Chem.RWMol, count: int) -> list[int]:
    chain: list[int] = []
    for _ in range(count):
        idx = rw.AddAtom(Chem.Atom(6))
        rw.GetAtomWithIdx(idx).SetIsAromatic(False)
        chain.append(idx)
    for i in range(count - 1):
        rw.AddBond(chain[i], chain[i + 1], Chem.BondType.SINGLE)
    return chain


def expand_methylene_thin(rw: Chem.RWMol, dummy_idx: int, count: int) -> dict:
    """Replace one CH2 dummy by a k-carbon linker (thin adapter; no assemble)."""
    if count < 1:
        raise ValueError("count must be >= 1")
    atom = rw.GetAtomWithIdx(dummy_idx)
    if atom.GetAtomicNum() != 0:
        raise ValueError("not a dummy")

    ends = [n.GetIdx() for n in atom.GetNeighbors()]
    degree = len(ends)
    if degree not in (1, 2):
        raise ValueError(f"CH2?k expansion unsupported for degree {degree}")

    for e in ends:
        rw.RemoveBond(dummy_idx, e)
    rw.RemoveAtom(dummy_idx)
    ends = [e - 1 if e > dummy_idx else e for e in ends]

    chain = _add_carbon_chain(rw, count)
    rw.AddBond(ends[0], chain[0], Chem.BondType.SINGLE)
    if degree == 2:
        rw.AddBond(chain[-1], ends[1], Chem.BondType.SINGLE)

    return {
        "degree": degree,
        "ends": ends,
        "chain": chain,
        "mode": "terminal" if degree == 1 else "bridge",
    }


def expand_numeric_ch2_in_esmiles(
    line: str,
    messages: list[str] | None = None,
) -> str:
    """Expand numeric ``CH2?k`` slots; leave ``CH2?n`` / ranges unchanged."""
    out, _mol = expand_numeric_ch2_in_esmiles_with_mol(line, messages)
    return out


def expand_numeric_ch2_in_esmiles_with_mol(
    line: str,
    messages: list[str] | None = None,
) -> tuple[str, Chem.Mol | None]:
    """Like :func:`expand_numeric_ch2_in_esmiles`, also return the working Mol."""
    log = messages if messages is not None else []
    text = (line or "").strip()
    if not text or "<sep>" not in text:
        return line, None

    loaded = load_esmiles(text, prefer_indigo=False)
    if loaded is None or loaded.mol is None:
        return line, None

    jobs: list[tuple[int, int, str]] = []
    for slot in loaded.anchor_slots:
        if slot.dummy_idx < 0:
            continue
        if is_abstract_ch2_label(slot.source_label):
            log.append(f"保留抽象亚甲基重复：{slot.source_label}")
            continue
        count = parse_ch2_count(slot.source_label)
        if count is None:
            continue
        jobs.append((slot.dummy_idx, count, slot.source_label))

    if not jobs:
        return text, loaded.mol

    rw = Chem.RWMol(Chem.Mol(loaded.mol))
    for dummy_idx, count, lab in sorted(jobs, key=lambda t: t[0], reverse=True):
        try:
            tr = expand_methylene_thin(rw, dummy_idx, count)
        except ValueError as exc:
            log.append(f"CH2 扩链跳过 {lab}：{exc}")
            continue
        log.append(f"亚甲基重复已展开：{lab} → (CH2){count}（{tr['mode']}）")

    mol = rw.GetMol()
    try:
        Chem.SanitizeMol(mol)
        Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    except Exception:
        log.append("CH2 扩链后 sanitize 失败，保留扩链前行。")
        return text, loaded.mol

    out = encode_substituent_esmiles(mol, remaining_dummy_rlabels(mol))
    return out, mol


def abstract_methylene_bridge_specs(line: str) -> list[MethyleneRepeatSpec]:
    """Return structured specs for supported degree-two abstract CH2 slots.

    Degree-one slots deliberately remain unsupported: with only one fixed endpoint
    the target does not define where the repeated chain ends.
    """
    loaded = load_esmiles(line, prefer_indigo=False)
    if loaded is None or loaded.mol is None:
        return []
    specs: list[MethyleneRepeatSpec] = []
    for slot in loaded.anchor_slots:
        if not is_abstract_ch2_label(slot.source_label) or slot.dummy_idx < 0:
            continue
        atom = loaded.mol.GetAtomWithIdx(slot.dummy_idx)
        endpoints = tuple(
            n.GetIdx() for n in atom.GetNeighbors() if n.GetAtomicNum() > 1
        )
        if len(endpoints) != 2:
            continue
        bounds = _abstract_ch2_bounds(slot.source_label)
        if bounds is None:
            continue
        specs.append(
            MethyleneRepeatSpec(
                internal_id=slot.internal_id,
                source_label=slot.source_label,
                dummy_idx=slot.dummy_idx,
                endpoint_idxs=(endpoints[0], endpoints[1]),
                min_count=bounds[0],
                max_count=bounds[1],
            )
        )
    return specs


def _abstract_ch2_bounds(label: str) -> tuple[int, int | None] | None:
    lab = (label or "").strip()
    m = _CH2_RANGE.match(lab)
    if m:
        lo_text, hi_text = m.group(1).split("-", 1)
        lo, hi = int(lo_text), int(hi_text)
        if lo < 1 or hi < lo:
            return None
        return lo, hi
    if _CH2_LETTER.match(lab):
        return 1, None
    return None


def _concretize_abstract_ch2(
    line: str,
    specs: list[MethyleneRepeatSpec],
    counts: tuple[int, ...],
) -> tuple[str, Chem.Mol | None, dict[str, tuple[int, ...]]]:
    """Expand one assignment while retaining slot identity on inserted atoms."""
    loaded = load_esmiles(line, prefer_indigo=False)
    if loaded is None or loaded.mol is None:
        return line, None, {}
    count_by_id = {spec.internal_id: count for spec, count in zip(specs, counts)}
    jobs = []
    for slot in loaded.anchor_slots:
        count = count_by_id.get(slot.internal_id)
        if count is not None:
            jobs.append((slot.dummy_idx, count, slot.internal_id))
    rw = Chem.RWMol(Chem.Mol(loaded.mol))
    for dummy_idx, count, internal_id in sorted(jobs, reverse=True):
        try:
            trace = expand_methylene_thin(rw, dummy_idx, count)
        except (IndexError, ValueError):
            # The loader may expose malformed or duplicated historical anchor
            # metadata. Such an assignment is unsupported evidence, not a
            # reason to abort the entire case/corpus run.
            return line, None, {}
        for chain_idx in trace["chain"]:
            atom = rw.GetAtomWithIdx(chain_idx)
            atom.SetProp("_esmiles_ch2_repeat_slot", internal_id)
    mol = rw.GetMol()
    try:
        Chem.SanitizeMol(mol)
        Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    except Exception:
        return line, None, {}
    chain_by_id: dict[str, list[int]] = {}
    for atom in mol.GetAtoms():
        if atom.HasProp("_esmiles_ch2_repeat_slot"):
            chain_by_id.setdefault(atom.GetProp("_esmiles_ch2_repeat_slot"), []).append(
                atom.GetIdx()
            )
    out = encode_substituent_esmiles(mol, remaining_dummy_rlabels(mol))
    return out, mol, {key: tuple(value) for key, value in chain_by_id.items()}


def _target_atom_is_strict_ch2(atom: Chem.Atom) -> bool:
    return (
        atom.GetAtomicNum() == 6
        and not atom.GetIsAromatic()
        and atom.GetFormalCharge() == 0
        and atom.GetNumRadicalElectrons() == 0
        and atom.GetTotalNumHs(includeNeighbors=True) == 2
        and sum(1 for n in atom.GetNeighbors() if n.GetAtomicNum() > 1) == 2
        and all(b.GetBondType() == Chem.BondType.SINGLE for b in atom.GetBonds())
    )


def _unmatched_atoms_only_at_open_slots(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    target_by_template: dict[int, int],
) -> bool:
    """Do not explain arbitrary leftover target atoms by choosing a shorter n.

    Unmapped target structure is legal only across a template atom that still
    neighbours a non-repeat dummy (an ordinary R/Markush attachment slot).
    """
    open_template_anchors = {
        n.GetIdx()
        for atom in template_mol.GetAtoms()
        if atom.GetAtomicNum() == 0
        for n in atom.GetNeighbors()
        if n.GetAtomicNum() > 1
    }
    mapped_target = set(target_by_template.values())
    template_by_target = {
        target_idx: template_idx
        for template_idx, target_idx in target_by_template.items()
    }
    for target_idx in mapped_target:
        for nbr in target_mol.GetAtomWithIdx(target_idx).GetNeighbors():
            if nbr.GetAtomicNum() <= 1 or nbr.GetIdx() in mapped_target:
                continue
            if template_by_target[target_idx] not in open_template_anchors:
                return False
    return True


def resolve_abstract_methylene_bridges(
    line: str,
    target: str | Chem.Mol,
    *,
    max_repeat_count: int | None = None,
    max_candidates: int = 256,
) -> list[ResolvedMethyleneTemplate]:
    """Infer abstract degree-two CH2 repeats by concrete target embedding.

    Each proposed count is expanded with the production thin adapter.  Only a
    full concrete-template substructure match whose inserted atoms are strict
    CH2 atoms is accepted.  This also handles a bridge that closes a ring.
    """
    specs = abstract_methylene_bridge_specs(line)
    if not specs:
        return []
    target_mol = (
        Chem.MolFromSmiles(target) if isinstance(target, str) else Chem.Mol(target)
    )
    if target_mol is None:
        return []
    natural_limit = max(1, target_mol.GetNumHeavyAtoms())
    cap = min(natural_limit, max_repeat_count) if max_repeat_count else natural_limit
    domains: list[range] = []
    for spec in specs:
        hi = min(cap, spec.max_count if spec.max_count is not None else cap)
        if hi < spec.min_count:
            return []
        domains.append(range(spec.min_count, hi + 1))

    from chemistry.align import template_heavy_query_mol

    results: list[ResolvedMethyleneTemplate] = []
    seen: set[tuple[str, tuple[tuple[str, int], ...]]] = set()
    for counts in product(*domains):
        concrete, template_mol, chain_by_id = _concretize_abstract_ch2(
            line, specs, counts
        )
        if template_mol is None:
            continue
        query, query_to_template = template_heavy_query_mol(template_mol)
        if query is None:
            continue
        for hit in target_mol.GetSubstructMatches(query, uniquify=False):
            target_by_template = {
                template_idx: int(hit[query_idx])
                for query_idx, template_idx in query_to_template.items()
            }
            if not _unmatched_atoms_only_at_open_slots(
                template_mol, target_mol, target_by_template
            ):
                continue
            assignments: list[MethyleneRepeatAssignment] = []
            valid = True
            for spec, count in zip(specs, counts):
                template_chain = chain_by_id.get(spec.internal_id, ())
                target_chain = tuple(
                    target_by_template.get(idx, -1) for idx in template_chain
                )
                if len(target_chain) != count or any(idx < 0 for idx in target_chain):
                    valid = False
                    break
                if not all(
                    _target_atom_is_strict_ch2(target_mol.GetAtomWithIdx(idx))
                    for idx in target_chain
                ):
                    valid = False
                    break
                assignments.append(
                    MethyleneRepeatAssignment(spec.internal_id, count, target_chain)
                )
            if not valid:
                continue
            assignment_key = tuple((a.internal_id, a.count) for a in assignments)
            key = (Chem.MolToSmiles(template_mol, isomericSmiles=True), assignment_key)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                ResolvedMethyleneTemplate(
                    esmiles=concrete,
                    mol=template_mol,
                    assignments=tuple(assignments),
                    messages=[
                        "抽象亚甲基桥已具体化："
                        + "，".join(f"{a.internal_id}={a.count}" for a in assignments)
                    ],
                )
            )
            if len(results) >= max_candidates:
                return results
    return results
