"""Fail-closed Extended-SMILES semantic audit shared by MCP and V2 assembly."""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from rdkit import Chem

_ROOT = Path(__file__).resolve().parents[1]
_FHR_SRC = _ROOT / "third_party" / "molecular_assembly_v2" / "src"
if str(_FHR_SRC) not in sys.path:
    sys.path.insert(0, str(_FHR_SRC))


def audit_esmiles(smiles: str) -> dict:
    """Report full extension semantics without rewriting the input string.

    ``semantic_valid`` means the loader accepts every tag/label. ``concrete_valid``
    additionally requires all abbreviations to expand and no dummy atom to remain.
    Open Markush labels are semantically valid protocol data but are never concrete.
    """
    text = (smiles or "").strip()
    if "<sep>" not in text:
        mol = Chem.MolFromSmiles(text) if text else None
        remaining = (
            sum(atom.GetAtomicNum() == 0 for atom in mol.GetAtoms())
            if mol is not None else None
        )
        return {
            "present": False,
            "semantic_valid": mol is not None,
            "concrete_valid": bool(mol is not None and remaining == 0),
            "unsupported_labels": [],
            "open_markush_labels": [],
            "unknown_tags": [],
            "issues": [] if mol is not None else ["SMILES body parse failed"],
            "expanded": text,
            "remaining_dummies": remaining,
        }

    from esmiles import load_esmiles, validate_esmiles_line
    from esmiles.abbrev import (
        _is_open_markush_label,
        expand_template_abbreviations,
        lookup_abbreviation_smiles,
    )

    loaded = load_esmiles(text)
    issues = validate_esmiles_line(text)
    if loaded is None:
        return {
            "present": True,
            "semantic_valid": False,
            "concrete_valid": False,
            "unsupported_labels": [],
            "open_markush_labels": [],
            "unknown_tags": [],
            "issues": issues or ["Extended-SMILES loader failed"],
            "expanded": text,
            "remaining_dummies": None,
            "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }

    known = []
    open_labels = []
    unsupported = []
    for tag in loaded.a_tags:
        label = tag.value.strip()
        if _is_open_markush_label(label):
            open_labels.append(label)
        elif lookup_abbreviation_smiles(label):
            known.append(label)
        else:
            unsupported.append(label)

    _body, extension = text.split("<sep>", 1)
    tag_names = set(re.findall(r"</?([A-Za-z][A-Za-z0-9_-]*)\b", extension))
    unknown_tags = sorted(tag_names - {"a", "c", "d", "v", "g", "r", "dum"})
    notes = []
    expanded = expand_template_abbreviations(text, notes)
    expanded_body = expanded.split("<sep>", 1)[0]
    expanded_mol = Chem.MolFromSmiles(expanded_body) if expanded_body else None
    remaining = (
        sum(atom.GetAtomicNum() == 0 for atom in expanded_mol.GetAtoms())
        if expanded_mol is not None else None
    )
    semantic_valid = not issues and not unsupported and not unknown_tags
    concrete_valid = bool(
        semantic_valid
        and not open_labels
        and expanded_mol is not None
        and remaining == 0
    )
    return {
        "present": True,
        "semantic_valid": semantic_valid,
        "concrete_valid": concrete_valid,
        "known_abbreviations": sorted(set(known)),
        "unsupported_labels": sorted(set(unsupported)),
        "open_markush_labels": sorted(set(open_labels)),
        "unknown_tags": unknown_tags,
        "issues": issues,
        "expanded": expanded,
        "remaining_dummies": remaining,
        "messages": notes,
        "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
