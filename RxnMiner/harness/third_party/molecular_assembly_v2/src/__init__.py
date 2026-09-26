"""Core library re-exports (prefer ``from chemistry import …`` / ``from esmiles import …``)."""

from chemistry.assembly import AssemblyResult, assemble_fragments, draw_single_fragment, parse_fragment_line
from chemistry.decompose import (
    AnchorInfo,
    MatchCandidate,
    PeelResult,
    Step0MapResult,
    peel_substituents,
    step0_scaffold_map,
)
from chemistry.models import AlignmentMethodSummary, BondMismatch, StereoMismatch

__all__ = [
    "AlignmentMethodSummary",
    "AnchorInfo",
    "AssemblyResult",
    "BondMismatch",
    "MatchCandidate",
    "PeelResult",
    "Step0MapResult",
    "StereoMismatch",
    "assemble_fragments",
    "draw_single_fragment",
    "parse_fragment_line",
    "peel_substituents",
    "step0_scaffold_map",
]
