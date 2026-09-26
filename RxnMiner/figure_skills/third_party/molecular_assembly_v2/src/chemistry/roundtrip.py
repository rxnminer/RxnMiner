"""Decompose → assemble round-trip and optional library checks."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rdkit import Chem

from chemistry.assembly import assemble_fragments, mol_to_png_base64, normalize_target_smiles
from chemistry.assembly_export import fragments_from_peel
from esmiles import ESMILES_SEPARATOR, load_esmiles, parse_fragment_line
from chemistry.models import (
    AnchorInfo,
    ENABLE_STEP0_ROUNDTRIP,
    H_SUBST_SMILES,
    PeelResult,
    PeeledSubstituent,
    ROUNDTRIP_DISABLED_NOTE,
)
from chemistry.peel_topology import PLACEHOLDER_Z, is_attachment_placeholder
from chemistry.r_groups import placeholder_isotope_from_r_label

_MARKUSH_EMPTY_RING = re.compile(r"\(\)")


@dataclass
class RoundtripResult:
    ok: bool
    note: str = ""
    assembled_smiles: str = ""
    assembled_inchi_key: str = ""
    target_inchi_key: str = ""
    assembled_image_base64: str = ""
    assembly_messages: list[str] = field(default_factory=list)
    assembly_fragment_lines: list[str] = field(default_factory=list)
    assembly_success: bool = False
    assembly_partial: bool = False


@dataclass
class LibraryContext:
    markush_enumeration: dict[str, str] = field(default_factory=dict)
    reactant_esmiles: list[str] = field(default_factory=list)


@dataclass
class LibraryMatchResult:
    ok: bool | None
    notes: list[str] = field(default_factory=list)
    matched: list[dict[str, str]] = field(default_factory=list)
    mismatched: list[dict[str, str]] = field(default_factory=list)


def mol_inchi_key(mol: Chem.Mol | None) -> str:
    if mol is None:
        return ""
    try:
        return Chem.MolToInchiKey(mol)
    except Exception:
        return ""


def compare_mols_inchi(target_mol: Chem.Mol, assembled_mol: Chem.Mol) -> tuple[bool, str, str, str]:
    """Match by InChIKey (stereo); fallback to canonical isomeric SMILES."""
    key_t = mol_inchi_key(target_mol)
    key_a = mol_inchi_key(assembled_mol)
    if key_t and key_a and key_t == key_a:
        return True, f"InChIKey 一致（{key_t}）", key_t, key_a
    can_t = Chem.MolToSmiles(target_mol, canonical=True, isomericSmiles=True)
    can_a = Chem.MolToSmiles(assembled_mol, canonical=True, isomericSmiles=True)
    if can_t and can_a and can_t == can_a:
        return True, "canonical isomeric SMILES 一致", key_t, key_a
    short_t = can_t if len(can_t) <= 60 else can_t[:57] + "..."
    short_a = can_a if len(can_a) <= 60 else can_a[:57] + "..."
    return (
        False,
        f"产物不一致：目标 {short_t} ≠ 组装 {short_a}",
        key_t,
        key_a,
    )


def _labels_from_fragment_tags(line: str) -> set[str]:
    parsed = parse_fragment_line(line)
    if parsed is None:
        return set()
    _, tags = parsed
    return {label for _, label in re.findall(r"<a>(\d+):([^<]+)</a>", tags)}


def _open_placeholder_labels(peel: PeelResult) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for sub in peel.substituents:
        label = (getattr(sub, "open_placeholder_label", "") or "").strip()
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def _relabel_first_scaffold_tag(line: str, source_label: str, target_label: str) -> str:
    parsed = parse_fragment_line(line)
    if parsed is None:
        return line
    body, tags = parsed
    replaced = False

    def repl(match: re.Match[str]) -> str:
        nonlocal replaced
        if replaced:
            return match.group(0)
        pos, label = match.group(1), match.group(2).strip()
        if label != source_label:
            return match.group(0)
        replaced = True
        return f"<a>{pos}:{target_label}</a>"

    new_tags = re.sub(r"<a>(\d+):([^<]+)</a>", repl, tags)
    return f"{body}{ESMILES_SEPARATOR}{new_tags}"


def _fragments_for_semigeneric_compare(
    peel: PeelResult,
    anchors: list[AnchorInfo],
    template_to_full: dict[int, int],
) -> tuple[list[str], list[str]]:
    """Roundtrip-only lines where open target labels stay open on the scaffold.

    The normal assembly path tries to connect every matching label. For a
    semigeneric target, a peeled ``Ar -> Ar[1]`` dummy means "the slot remains
    open as Ar[1]", not a concrete substituent to connect. This function creates
    a compare-only fragment set with the scaffold slot relabeled and the dummy
    substituent omitted.
    """
    lines = fragments_from_peel(peel, anchors, template_to_full)
    if not lines:
        return lines, []
    compare_lines = list(lines[:1])
    open_labels: list[str] = []
    for sub in peel.substituents:
        open_label = (getattr(sub, "open_placeholder_label", "") or "").strip()
        if open_label:
            source_label = (getattr(sub, "r_label", "") or "").strip()
            if source_label:
                compare_lines[0] = _relabel_first_scaffold_tag(
                    compare_lines[0],
                    source_label,
                    open_label,
                )
            open_labels.append(open_label)
            continue
        line = fragments_from_peel(
            PeelResult(ok=True, substituents=[sub]),
            anchors,
            template_to_full,
        )
        if line:
            compare_lines.extend(line[1:] if len(line) > 1 else line)
    # The loop above intentionally avoids reusing ``lines[1:]`` blindly because
    # open placeholders must be omitted from the compare-only assembly.
    if not compare_lines[1:]:
        for line in lines[1:]:
            if not any(label in _labels_from_fragment_tags(line) for label in open_labels):
                compare_lines.append(line)
    return compare_lines, open_labels


def _label_for_placeholder(atom: Chem.Atom, known_labels: list[str]) -> str:
    if atom.HasProp("_R_label"):
        return atom.GetProp("_R_label").strip()
    iso = atom.GetIsotope()
    for label in known_labels:
        if placeholder_isotope_from_r_label(label) == iso:
            return label
    return ""


def _canonical_partial_mol(mol: Chem.Mol, known_labels: list[str]) -> str:
    rw = Chem.RWMol(Chem.Mol(mol))
    remove_idxs: list[int] = []
    for atom in rw.GetAtoms():
        atom.SetAtomMapNum(0)
        if not is_attachment_placeholder(atom):
            continue
        label = _label_for_placeholder(atom, known_labels)
        if not label:
            remove_idxs.append(atom.GetIdx())
            continue
        atom.SetAtomicNum(0)
        atom.SetIsotope(placeholder_isotope_from_r_label(label) if label else 0)
    for bond in rw.GetBonds():
        if (
            bond.GetBondType() == Chem.BondType.DATIVE
            and (
                is_attachment_placeholder(rw.GetAtomWithIdx(bond.GetBeginAtomIdx()))
                or is_attachment_placeholder(rw.GetAtomWithIdx(bond.GetEndAtomIdx()))
            )
        ):
            bond.SetBondType(Chem.BondType.SINGLE)
        if (
            is_attachment_placeholder(rw.GetAtomWithIdx(bond.GetBeginAtomIdx()))
            or is_attachment_placeholder(rw.GetAtomWithIdx(bond.GetEndAtomIdx()))
        ):
            bond.SetBondDir(Chem.BondDir.NONE)
    duplicate_keys: dict[tuple[int, tuple[int, ...]], int] = {}
    for atom in rw.GetAtoms():
        if not is_attachment_placeholder(atom):
            continue
        iso = atom.GetIsotope()
        if not iso:
            continue
        neighbor_key = tuple(
            sorted(
                n.GetIdx()
                for n in atom.GetNeighbors()
                if not is_attachment_placeholder(n)
            )
        )
        key = (iso, neighbor_key)
        if key in duplicate_keys:
            remove_idxs.append(atom.GetIdx())
        else:
            duplicate_keys[key] = atom.GetIdx()
    for idx in sorted(remove_idxs, reverse=True):
        if 0 <= idx < rw.GetNumAtoms():
            rw.RemoveAtom(idx)
    out = rw.GetMol()
    try:
        Chem.SanitizeMol(out)
        Chem.AssignStereochemistry(out, cleanIt=True, force=True)
    except Exception:
        pass
    return Chem.MolToSmiles(out, canonical=True, isomericSmiles=True)


def compare_semigeneric_roundtrip(
    target_smiles: str,
    assembled_smiles: str,
    open_labels: list[str],
) -> tuple[bool | None, str]:
    labels = [label for label in open_labels if label]
    if not labels or ESMILES_SEPARATOR not in (target_smiles or ""):
        return None, ""
    target_loaded = load_esmiles(target_smiles, apply_dummy=True, prefer_indigo=False)
    if target_loaded is None or target_loaded.mol is None:
        return None, "半通式 target 无法加载，无法执行 partial equivalence。"
    assembled_mol = Chem.MolFromSmiles(assembled_smiles or "", sanitize=False)
    if assembled_mol is None:
        return None, "半通式组装结果无法加载，无法执行 partial equivalence。"
    target_open = _labels_from_fragment_tags(target_smiles)
    if not set(labels).issubset(target_open):
        return False, (
            "半通式开放位点不一致："
            f"target={sorted(target_open)}，roundtrip={sorted(set(labels))}"
        )
    can_t = _canonical_partial_mol(target_loaded.mol, labels)
    can_a = _canonical_partial_mol(assembled_mol, labels)
    if can_t == can_a:
        return True, f"半通式 partial equivalence 通过（开放位点：{', '.join(labels)}）"
    return False, f"半通式 partial equivalence 失败：目标 {can_t} ≠ 组装 {can_a}"


def skipped_roundtrip_result() -> RoundtripResult:
    return RoundtripResult(ok=False, note=ROUNDTRIP_DISABLED_NOTE)


def verify_roundtrip(
    target_smiles: str,
    peel: PeelResult,
    anchors: list[AnchorInfo],
    template_to_full: dict[int, int],
    *,
    render_images: bool = False,
) -> RoundtripResult:
    if not ENABLE_STEP0_ROUNDTRIP:
        return skipped_roundtrip_result()

    lines = fragments_from_peel(peel, anchors, template_to_full)
    if not lines:
        return RoundtripResult(ok=False, note="无可用组装片段", assembly_fragment_lines=lines)

    asm = assemble_fragments(lines)
    target_smi, _ = normalize_target_smiles(target_smiles)
    target_mol = Chem.MolFromSmiles(target_smi)
    if target_mol is None:
        compare_lines, open_labels = _fragments_for_semigeneric_compare(
            peel, anchors, template_to_full
        )
        if open_labels:
            partial_asm = assemble_fragments(compare_lines)
            partial_ok, partial_note = compare_semigeneric_roundtrip(
                target_smi,
                partial_asm.smiles,
                open_labels,
            )
            if partial_ok is not None:
                img = partial_asm.image_png_base64
                if render_images and not img and partial_asm.smiles:
                    mol = Chem.MolFromSmiles(partial_asm.smiles, sanitize=False)
                    if mol is not None:
                        img = mol_to_png_base64(mol)
                return RoundtripResult(
                    ok=bool(partial_ok),
                    note=partial_note,
                    assembled_smiles=partial_asm.smiles,
                    assembled_image_base64=img if render_images else "",
                    assembly_messages=partial_asm.messages,
                    assembly_fragment_lines=compare_lines,
                    assembly_success=partial_asm.success,
                    assembly_partial=partial_asm.partial,
                )
        return RoundtripResult(
            ok=False,
            note=f"无法解析目标 SMILES: {target_smi[:80]}",
            assembly_fragment_lines=lines,
            assembly_messages=asm.messages,
        )

    assembled_mol: Chem.Mol | None = None
    if asm.smiles:
        assembled_mol = Chem.MolFromSmiles(asm.smiles)

    if assembled_mol is None or not asm.success:
        return RoundtripResult(
            ok=False,
            note="组装失败或仍有未连接位点",
            assembled_smiles=asm.smiles,
            assembly_messages=asm.messages,
            assembly_fragment_lines=lines,
            assembly_success=asm.success,
            assembly_partial=asm.partial,
            assembled_image_base64=asm.image_png_base64 if render_images else "",
            target_inchi_key=mol_inchi_key(target_mol),
        )

    ok, note, key_t, key_a = compare_mols_inchi(target_mol, assembled_mol)
    if not ok:
        compare_lines, open_labels = _fragments_for_semigeneric_compare(
            peel, anchors, template_to_full
        )
        if open_labels:
            partial_asm = assemble_fragments(compare_lines)
            partial_ok, partial_note = compare_semigeneric_roundtrip(
                target_smi,
                partial_asm.smiles,
                open_labels,
            )
            if partial_ok is not None:
                asm = partial_asm
                ok = bool(partial_ok)
                note = partial_note
                assembled_mol = Chem.MolFromSmiles(asm.smiles, sanitize=False) or assembled_mol
    img = asm.image_png_base64
    if render_images and not img:
        img = mol_to_png_base64(assembled_mol)
    return RoundtripResult(
        ok=ok,
        note=note,
        assembled_smiles=asm.smiles,
        assembled_inchi_key=key_a,
        target_inchi_key=key_t,
        assembled_image_base64=img if render_images else "",
        assembly_messages=asm.messages,
        assembly_fragment_lines=lines,
        assembly_success=asm.success,
        assembly_partial=asm.partial,
    )


def _normalize_markush_smiles(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    # Synfacts aromatic placeholder ``()`` = implicit ring carbon (e.g. phenyl).
    s = re.sub(r"ccc\(\)cc", "ccccc", s, flags=re.IGNORECASE)
    s = _MARKUSH_EMPTY_RING.sub("", s)
    return s


def _subst_core_mol(smiles_line: str) -> Chem.Mol | None:
    parsed = parse_fragment_line(smiles_line)
    if parsed is None:
        return None
    mol = Chem.MolFromSmiles(parsed[0])
    if mol is None:
        return None
    rw = Chem.RWMol(mol)
    remove = [a.GetIdx() for a in rw.GetAtoms() if a.GetAtomicNum() == 0]
    for idx in sorted(remove, reverse=True):
        rw.RemoveAtom(idx)
    try:
        out = rw.GetMol()
        Chem.SanitizeMol(out)
        return out
    except Exception:
        return None


def _core_inchi(smiles_line: str) -> str:
    mol = _subst_core_mol(smiles_line)
    return mol_inchi_key(mol)


def verify_library_match(
    substituents: list[PeeledSubstituent] | list[dict[str, Any]],
    library: LibraryContext | None,
) -> LibraryMatchResult:
    if not ENABLE_STEP0_ROUNDTRIP:
        return LibraryMatchResult(ok=None, notes=[ROUNDTRIP_DISABLED_NOTE])
    if library is None or not library.markush_enumeration:
        return LibraryMatchResult(ok=None, notes=["未提供 markush_enumeration"])

    markush = library.markush_enumeration
    matched: list[dict[str, str]] = []
    mismatched: list[dict[str, str]] = []
    notes: list[str] = []

    for item in substituents:
        if isinstance(item, PeeledSubstituent):
            r_label = item.r_label
            smi = item.smiles
            heavy = item.heavy_atoms
        else:
            r_label = str(item.get("r_label") or "")
            smi = str(item.get("smiles") or "")
            heavy = int(item.get("heavy_atoms") or 0)
        if heavy <= 0 or not smi or smi == H_SUBST_SMILES:
            continue
        expected = markush.get(r_label, "").strip()
        if not expected:
            for slot in item.get("slot_ids", []) if isinstance(item, dict) else item.slot_ids:
                expected = markush.get(slot, "").strip()
                if expected:
                    r_label = slot
                    break
        if not expected:
            notes.append(f"{r_label or '?'}：库中无对应条目，跳过")
            continue
        exp_norm = _normalize_markush_smiles(expected)
        exp_mol = Chem.MolFromSmiles(exp_norm) if exp_norm else None
        exp_key = mol_inchi_key(exp_mol) if exp_mol else ""
        got_key = _core_inchi(smi)
        if exp_key and got_key and exp_key == got_key:
            matched.append({"r_label": r_label, "expected": expected, "peeled": smi})
            continue
        got_mol = _subst_core_mol(smi)
        got_can = Chem.MolToSmiles(got_mol, canonical=True, isomericSmiles=True) if got_mol else ""
        exp_can = (
            Chem.MolToSmiles(exp_mol, canonical=True, isomericSmiles=True) if exp_mol else ""
        )
        if got_can and exp_can and got_can == exp_can:
            matched.append({"r_label": r_label, "expected": expected, "peeled": smi})
        else:
            mismatched.append(
                {
                    "r_label": r_label,
                    "expected": expected,
                    "peeled": smi,
                    "expected_can": exp_can,
                    "peeled_can": got_can,
                }
            )

    if mismatched:
        return LibraryMatchResult(
            ok=False,
            notes=notes + [f"{m['r_label']}: 期望 {m['expected']!r} ≠ 扣得 {m['peeled_can']!r}" for m in mismatched],
            matched=matched,
            mismatched=mismatched,
        )
    if not matched:
        return LibraryMatchResult(ok=None, notes=notes or ["无可对照的非 H 取代基"])
    return LibraryMatchResult(ok=True, notes=notes, matched=matched, mismatched=mismatched)
