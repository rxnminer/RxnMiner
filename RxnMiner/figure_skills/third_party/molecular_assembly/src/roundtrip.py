"""Decompose → assemble round-trip and optional library checks."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rdkit import Chem

from assembly import assemble_fragments, mol_to_png_base64, normalize_target_smiles
from assembly_export import fragments_from_peel
from esmiles_loader import parse_fragment_line
from models import (
    AnchorInfo,
    ENABLE_STEP0_ROUNDTRIP,
    H_SUBST_SMILES,
    PeelResult,
    PeeledSubstituent,
    ROUNDTRIP_DISABLED_NOTE,
)

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
