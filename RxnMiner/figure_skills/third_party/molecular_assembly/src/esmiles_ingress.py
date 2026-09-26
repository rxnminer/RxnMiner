"""Unified eSMILES ingress: abbreviation expansion and target mode classification.

See ``docs/ESMILES_INGRESS.md`` for design rationale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rdkit import Chem

from abbrev_expand import expand_template_abbreviations
from esmiles_loader import split_esmiles
from r_groups import _R_BRACKET, _R_PLAIN, parse_tag_string

TargetMode = Literal["concrete", "fragment"]

_R_DIGIT = re.compile(r"^R\d+(['′″‴]|#\d+)?$", re.IGNORECASE)


@dataclass(frozen=True)
class NormalizedTarget:
    """Normalized product-side input for Step0 / reverse assembly."""

    smiles: str
    mode: TargetMode
    note: str | None = None


def is_markush_slot_label(label: str) -> bool:
    """True when ``<a>`` label names an open R-slot / Markush variable (not a group abbrev)."""
    lab = (label or "").strip()
    if not lab or lab == "<dum>":
        return True
    if lab.lower() == "ar":
        return True
    if _R_BRACKET.match(lab) or _R_PLAIN.match(lab) or _R_DIGIT.match(lab):
        return True
    return False


def extension_has_markush_slots(extension: str) -> bool:
    """Whether extension ``<a>`` tags include at least one open Markush slot label."""
    if not extension or "<a>" not in extension:
        return False
    return any(is_markush_slot_label(lbl) for _, lbl in parse_tag_string(extension))


def classify_target_esmiles(line: str) -> TargetMode:
    """Classify product input after abbreviation expansion.

    * **concrete** — complete molecule (plain SMILES or body without ``*``).
    * **fragment** — partial Markush / nested instance; body still has ``*`` and
      suffix carries R-slot labels.
    """
    text = (line or "").strip()
    if not text or "<sep>" not in text:
        return "concrete"

    body, extension = split_esmiles(text)
    if not extension:
        return "concrete"
    if "*" not in body:
        return "concrete"
    if extension_has_markush_slots(extension):
        return "fragment"
    return "concrete"


def normalize_fragment_esmiles(
    line: str,
    messages: list[str] | None = None,
) -> str:
    """Expand abbreviations on a fragment / template eSMILES line; keep ``<sep>`` suffix."""
    return expand_template_abbreviations((line or "").strip(), messages)


def normalize_target_input(smiles: str) -> NormalizedTarget:
    """Normalize product-side SMILES / eSMILES for Step0 and reverse assembly."""
    s = (smiles or "").strip()
    if not s:
        return NormalizedTarget("", "concrete", None)
    if "<sep>" not in s:
        return NormalizedTarget(s, "concrete", None)

    expand_notes: list[str] = []
    expanded = expand_template_abbreviations(s, expand_notes)

    if "<sep>" not in expanded:
        if expanded != s and expand_notes:
            return NormalizedTarget(expanded, "concrete", expand_notes[0])
        if "<sep>" not in s:
            return NormalizedTarget(s, "concrete", None)
        note = expand_notes[0] if expand_notes else "缩写展开后已为完整 SMILES。"
        return NormalizedTarget(expanded, "concrete", note)

    mode = classify_target_esmiles(expanded)

    if mode == "fragment":
        note = expand_notes[0] if expand_notes else "产物为 fragment / 部分 Markush，保留 <sep> 与连接位标签。"
        return NormalizedTarget(expanded, "fragment", note)

    body, suffix = split_esmiles(expanded)
    if not suffix:
        note = "已自动去掉完整分子末尾的 <sep>。"
        return NormalizedTarget(body, "concrete", note)

    if body and Chem.MolFromSmiles(body) is not None:
        if expanded != s and expand_notes:
            return NormalizedTarget(body, "concrete", expand_notes[0])
        short = suffix if len(suffix) <= 48 else suffix[:45] + "…"
        if expanded != s:
            return NormalizedTarget(
                body,
                "concrete",
                expand_notes[0] if expand_notes else "产物 fragment 后缀已展开为具体结构。",
            )
        return NormalizedTarget(
            body,
            "concrete",
            f"完整分子含 fragment 后缀（{short}），已仅用 SMILES 主体。",
        )

    return NormalizedTarget(expanded, "concrete", expand_notes[0] if expand_notes else None)


def load_target_mol(
    normalized: NormalizedTarget,
) -> tuple[Chem.Mol | None, list[str]]:
    """Build an RDKit mol from a :class:`NormalizedTarget`."""
    notes: list[str] = []
    if not normalized.smiles:
        return None, notes

    if normalized.mode == "fragment":
        from esmiles_loader import load_esmiles

        loaded = load_esmiles(normalized.smiles, apply_dummy=True, prefer_indigo=True)
        if loaded is None or loaded.mol is None:
            return None, notes
        notes.extend(loaded.messages)
        notes.append("产物按 fragment eSMILES 加载（可含开放连接位 *）。")
        return loaded.mol, notes

    mol = Chem.MolFromSmiles(normalized.smiles)
    return mol, notes
