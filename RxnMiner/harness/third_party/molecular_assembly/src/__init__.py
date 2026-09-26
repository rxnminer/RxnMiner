"""Core library: assembly, Step0 decompose (split modules), peel topology, R tags."""

from assembly import AssemblyResult, assemble_fragments, draw_single_fragment, parse_fragment_line
from decompose import (
    AnchorInfo,
    MatchCandidate,
    PeelResult,
    Step0MapResult,
    peel_substituents,
    step0_scaffold_map,
)
from models import AlignmentMethodSummary, BondMismatch, StereoMismatch

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
