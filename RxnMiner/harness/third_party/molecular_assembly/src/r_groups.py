"""Synfacts / DP eSMILES R connection labels from ``<a>pos:label</a>`` tags.

Slot names come from upstream tags (``Ar``, ``R[1]``, ``R'`` …), not invented
``r1``/``r2`` ids. ``slot_key`` disambiguates duplicate labels (occurrence > 1).
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

_TAG_PATTERN = re.compile(r"<a>(\d+):([^<]+)</a>")
_R_BRACKET = re.compile(r"^R\[([^\]]+)\](['′″‴]|#\d+)?$", re.IGNORECASE)
_R_PLAIN = re.compile(r"^R(['′″‴]|#\d+)?$", re.IGNORECASE)
_R_BRACKET_INNER_DIGIT = re.compile(r"^\d+$")

_DEFAULT_GENERIC_R_ISOTOPE = 99
_HASH_ISOTOPE_MAX = 98


@dataclass(frozen=True)
class RTagSlot:
    dummy_idx: int
    source_label: str
    internal_id: str
    display_label: str
    pair_key: str
    occurrence: int
    smiles_position: int = -1

    @property
    def slot_key(self) -> str:
        return self.internal_id


def parse_tag_string(tags: str) -> list[tuple[int, str]]:
    return [(int(d), lbl.strip()) for d, lbl in _TAG_PATTERN.findall(tags)]


def pair_key_from_source(source_label: str) -> str:
    return source_label.strip()


def short_display_from_source(source_label: str) -> str:
    """Compact on-structure text: ``R[4]'`` → ``R4'``."""
    raw = (source_label or "").strip()
    m = _R_BRACKET.match(raw)
    if m:
        inner = m.group(1).strip()
        suffix = m.group(2) or ""
        if _R_BRACKET_INNER_DIGIT.match(inner):
            return f"R{inner}{suffix}"
    return raw


def slot_key_from_label(source_label: str, occurrence: int) -> str:
    """Unique slot id for peel tracking; display still uses ``source_label``."""
    label = (source_label or "").strip()
    if occurrence <= 1:
        return label
    return f"{label}#{occurrence}"


def group_display_label(slot_keys: list[str]) -> str:
    """Peeled substituent touching multiple slots → ``Ar+R[1]``."""

    def sort_key(s: str) -> tuple[int, str]:
        m = re.match(r"^(.+)#(\d+)$", s)
        if m:
            return (int(m.group(2)), m.group(1))
        return (0, s)

    return "+".join(sorted(set(slot_keys), key=sort_key))


def normalize_r_tags(tags: str) -> list[RTagSlot]:
    slots: list[RTagSlot] = []
    pair_counts: dict[str, int] = {}
    for dummy_pos, source in parse_tag_string(tags):
        pk = pair_key_from_source(source)
        pair_counts[pk] = pair_counts.get(pk, 0) + 1
        occ = pair_counts[pk]
        sk = slot_key_from_label(source, occ)
        slots.append(
            RTagSlot(
                dummy_idx=dummy_pos,
                source_label=source,
                internal_id=sk,
                display_label=short_display_from_source(source),
                pair_key=pk,
                occurrence=occ,
                smiles_position=dummy_pos,
            )
        )
    return slots


def slot_by_internal(slots: list[RTagSlot], slot_key: str) -> RTagSlot | None:
    for s in slots:
        if s.internal_id == slot_key or s.slot_key == slot_key:
            return s
    return None


def placeholder_isotope_from_r_label(label: str) -> int:
    """Map connection label to Ts placeholder isotope (CIP discrimination)."""
    raw = (label or "").strip()
    if not raw:
        return _DEFAULT_GENERIC_R_ISOTOPE
    m = _R_BRACKET.match(raw)
    if m:
        inner = m.group(1).strip()
        if _R_BRACKET_INNER_DIGIT.match(inner):
            return max(1, min(int(inner), 250))
        return _stable_label_isotope(raw)
    if _R_PLAIN.match(raw):
        return _DEFAULT_GENERIC_R_ISOTOPE
    return _stable_label_isotope(raw)


def _stable_label_isotope(label: str) -> int:
    h = zlib.crc32(label.encode("utf-8")) & 0xFFFFFFFF
    return (h % _HASH_ISOTOPE_MAX) + 1
