"""Compile small textual Markush fragments into Extended-SMILES.

This module intentionally handles only deterministic, binary fragments such as
``R³-Bpin``, ``R⁶-X`` and ``PhCl``.  It is not a general chemical-name parser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from chemistry.graft import star_body_from_group_smiles

from .abbrev import lookup_abbreviation_smiles
from .loader import load_esmiles

_SUPER = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_DASH_RE = re.compile(r"\s*[‐‑‒–—−-]\s*")
_R_RE = re.compile(r"^R(?:\[(\d+)\]|(\d+))$", re.I)
_ELEMENTS = {"F", "Cl", "Br", "I"}


@dataclass(frozen=True)
class TextMarkushResult:
    source: str
    normalized: str
    esmiles: str
    open_labels: tuple[str, ...] = ()
    fixed_groups: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    confidence: float = 1.0


def _normalise_label(token: str) -> str:
    raw = token.strip().translate(_SUPER).replace(" ", "")
    match = _R_RE.fullmatch(raw)
    return f"R[{match.group(1) or match.group(2)}]" if match else raw


def _fixed_smiles(token: str) -> str:
    if token in _ELEMENTS:
        return token
    return lookup_abbreviation_smiles(token)


def _binary_tokens(text: str) -> tuple[str, str]:
    normalized = text.strip().translate(_SUPER).replace(" ", "")
    split = _DASH_RE.split(normalized)
    if len(split) == 2 and all(split):
        return split[0], split[1]
    # Common compact form: a known abbreviation followed by a halogen.
    for element in sorted(_ELEMENTS, key=len, reverse=True):
        if normalized.endswith(element) and len(normalized) > len(element):
            return normalized[: -len(element)], element
    raise ValueError(f"unsupported textual Markush fragment: {text!r}")


def compile_simple_text_markush(
    text: str, *, constraints: dict[str, str] | None = None
) -> TextMarkushResult:
    """Compile a deterministic two-ended textual fragment.

    Constraints specialize page variables, e.g. ``{"X": "I"}`` for
    ``R⁶-X``. Unknown unconstrained symbols remain named open slots.
    """
    left_raw, right_raw = _binary_tokens(text)
    substitutions = {k.upper(): v for k, v in (constraints or {}).items()}
    tokens = [_normalise_label(left_raw), _normalise_label(right_raw)]
    tokens = [substitutions.get(token.upper(), token) for token in tokens]

    open_labels: list[str] = []
    fixed: list[str] = []
    fixed_smiles: list[str] = []
    for token in tokens:
        smi = _fixed_smiles(token)
        if smi:
            fixed.append(token)
            fixed_smiles.append(smi)
        else:
            open_labels.append(token)
            fixed_smiles.append("")

    if len(open_labels) == 1 and len(fixed) == 1:
        open_index = 0 if not fixed_smiles[0] else 1
        body = star_body_from_group_smiles(fixed_smiles[1 - open_index])
        esmiles = f"{body}<sep><a>0:{tokens[open_index]}</a>"
    elif len(open_labels) == 2:
        esmiles = f"**<sep><a>0:{tokens[0]}</a><a>1:{tokens[1]}</a>"
    elif len(fixed) == 2:
        # Join two concrete one-ended fragments through their dummy atoms.
        from chemistry.graft import assemble_labeled_fragments
        lines = [
            f"{star_body_from_group_smiles(fixed_smiles[0])}<sep><a>0:__join</a>",
            f"{star_body_from_group_smiles(fixed_smiles[1])}<sep><a>0:__join</a>",
        ]
        mol = assemble_labeled_fragments(lines, [])
        if mol is None:
            raise ValueError(f"could not join fixed groups in {text!r}")
        from rdkit import Chem
        esmiles = Chem.MolToSmiles(mol, isomericSmiles=True)
    else:
        raise ValueError(f"unsupported textual Markush fragment: {text!r}")

    loaded = load_esmiles(esmiles)
    if loaded.mol is None:
        raise ValueError(f"compiled invalid Extended-SMILES for {text!r}: {esmiles}")
    return TextMarkushResult(
        source=text,
        normalized="-".join(tokens),
        esmiles=esmiles,
        open_labels=tuple(open_labels),
        fixed_groups=tuple(fixed),
        confidence=1.0 if not open_labels or all(label.startswith("R[") for label in open_labels) else 0.9,
    )
