"""Unified eSMILES ingress: abbreviation expansion and target mode classification.

See ``src/esmiles/README.md`` for design rationale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rdkit import Chem

from chemistry.r_groups import _R_BRACKET, _R_PLAIN, parse_tag_string

from .abbrev import expand_template_abbreviations
from .loader import split_esmiles

TargetMode = Literal["concrete", "fragment"]

_R_DIGIT = re.compile(r"^R\d+(['′″‴]|#\d+)?$", re.IGNORECASE)
_OPEN_MARKUSH_INDEXED = re.compile(r"^(Ar|Alk|HetAr)(\[\d+\]|\d+)$", re.IGNORECASE)
_OPEN_MARKUSH_LABELS = {"ar", "alk"}


@dataclass(frozen=True)
class NormalizedTarget:
    """Normalized product-side input for Step0 / reverse assembly."""

    smiles: str
    mode: TargetMode
    note: str | None = None


def is_markush_slot_label(label: str) -> bool:
    """True when ``<a>`` label names an open R-slot / Markush variable (not a group abbrev).

    After abbreviation expansion, leftover tagged ``*`` whose label is **not** a
    known concrete abbrev (e.g. ``NHQ``, ``Q``) remain open / nestable slots.
    Known abbrevs such as ``PMP`` / ``t-Bu`` stay False so fully-expanded products
    classify as concrete.
    """
    lab = (label or "").strip()
    if not lab or lab == "<dum>":
        return True
    if lab.lower() in _OPEN_MARKUSH_LABELS:
        return True
    if _OPEN_MARKUSH_INDEXED.match(lab):
        return True
    if _R_BRACKET.match(lab) or _R_PLAIN.match(lab) or _R_DIGIT.match(lab):
        return True
    from .abbrev import _lookup_abbrev_smiles

    return not bool(_lookup_abbrev_smiles(lab))


def extension_has_markush_slots(extension: str) -> bool:
    """Whether extension ``<a>`` tags include at least one open Markush slot label."""
    if not extension or "<a>" not in extension:
        return False
    return any(is_markush_slot_label(lbl) for _, lbl in parse_tag_string(extension))


def classify_target_esmiles(line: str) -> TargetMode:
    """Classify product input after abbreviation expansion.

    * **concrete** — complete molecule (plain SMILES or body without ``*``).
    * **fragment** — partial Markush / nested instance; body still has ``*`` and
      suffix carries open slot labels (``R`` / ``Ar`` / custom like ``NHQ``).
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
    raw = (line or "").strip()
    expanded = expand_template_abbreviations(raw, messages)
    raw_body, _raw_suffix = split_esmiles(raw)
    expanded_body, _expanded_suffix = split_esmiles(expanded)
    if ("/" in raw_body or "\\" in raw_body) and not (
        "/" in expanded_body or "\\" in expanded_body
    ):
        if messages is not None:
            messages.append("模板含 E/Z 方向键；缩写展开会丢失方向，已保留原始模板。")
        return raw
    return expanded


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
    body, suffix = split_esmiles(expanded)

    # Safety: leftover * after expansion must keep tags (nested / custom open slots).
    if mode == "concrete" and "*" in body and suffix and extension_has_markush_slots(suffix):
        mode = "fragment"

    if mode == "fragment":
        note = expand_notes[0] if expand_notes else "产物为 fragment / 部分 Markush，保留 <sep> 与连接位标签。"
        return NormalizedTarget(expanded, "fragment", note)

    if not suffix:
        note = "已自动去掉完整分子末尾的 <sep>。"
        return NormalizedTarget(body, "concrete", note)

    if body and Chem.MolFromSmiles(body) is not None:
        if "*" in body:
            return NormalizedTarget(
                expanded,
                "fragment",
                expand_notes[0] if expand_notes else "产物主体仍含 *，按 fragment 保留标签。",
            )
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
        from .loader import load_esmiles

        loaded = load_esmiles(normalized.smiles, apply_dummy=True, prefer_indigo=True)
        if loaded is None or loaded.mol is None:
            return None, notes
        notes.extend(loaded.messages)
        notes.append("产物按 fragment eSMILES 加载（可含开放连接位 *）。")
        return loaded.mol, notes

    mol = Chem.MolFromSmiles(normalized.smiles)
    return mol, notes
