"""Step0 dataclasses and shared constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rdkit import Chem

H_SUBST_SMILES = "*[H]"

BOND_TYPE_ZH: dict[Any, str] = {
    Chem.BondType.SINGLE: "单键",
    Chem.BondType.DOUBLE: "双键",
    Chem.BondType.TRIPLE: "三键",
    Chem.BondType.AROMATIC: "芳香",
}

R_GROUP_COLORS: list[tuple[float, float, float]] = [
    (0.95, 0.45, 0.45),
    (0.45, 0.75, 0.95),
    (0.55, 0.85, 0.45),
    (0.95, 0.75, 0.35),
    (0.75, 0.55, 0.95),
    (0.45, 0.9, 0.85),
    (0.9, 0.5, 0.7),
    (0.65, 0.65, 0.65),
]

SOURCE_SORT_KEY = {"B·子结构": 0, "A·MCS键级": 1, "A·MCS拓扑": 2}

SANITIZE_RELAXED = (
    Chem.SanitizeFlags.SANITIZE_ALL ^ Chem.SanitizeFlags.SANITIZE_KEKULIZE
)

# Peeled substituent fragments: incomplete rings — skip kekulize + aromaticity perception.
PEEL_FRAGMENT_SANITIZE = (
    Chem.SanitizeFlags.SANITIZE_ALL
    ^ Chem.SanitizeFlags.SANITIZE_KEKULIZE
    ^ Chem.SanitizeFlags.SANITIZE_SETAROMATICITY
)

# Step0: decompose → assemble → compare (roundtrip + markush library).
ENABLE_STEP0_ROUNDTRIP = True
ROUNDTRIP_DISABLED_NOTE = "组装验证已关闭（ENABLE_STEP0_ROUNDTRIP=False）。"


@dataclass
class AnchorInfo:
    template_dummy_idx: int
    template_anchor_idx: int
    r_label: str
    internal_id: str
    display_label: str
    pair_key: str
    occurrence: int
    heavy_degree: int
    anchor_symbol: str
    smiles_position: int = -1
    anchor_kind: str = "dummy"  # dummy | bridge | ring | virtual_arc
    secondary_anchor_idx: int = -1
    virtual_arc_index: int = -1
    bridge_anchor_idxs: tuple[int, ...] = ()


@dataclass
class PeeledSubstituent:
    display_label: str
    slot_ids: list[str]
    anchor_full_idxs: list[int]
    smiles: str
    heavy_atoms: int
    r_label: str = ""
    internal_id: str = ""
    anchor_full_idx: int = -1
    image_base64: str = ""
    open_placeholder_label: str = ""


@dataclass(frozen=True)
class DummySite:
    """One Ts placeholder on the peeled scaffold (supports multiple per anchor)."""

    orig_anchor: int
    orig_leaf: int
    bond_type: Chem.BondType
    r_label: str = ""
    display_label: str = ""
    bridge_key: str = ""


@dataclass
class ChiralFrameSnapshot:
    """Stereo at a core atom: leaving ligand side vs template-ordered core neighbors."""

    center_full: int
    ordered_core_full: tuple[int, ...]
    leaving_full: int
    parity_sign: int


@dataclass
class PeelResult:
    ok: bool
    scaffold_smiles: str = ""
    scaffold_image_base64: str = ""
    scaffold_mol: Any = None  # Chem.Mol after peel; for stereo check (not serialized)
    target_labeled_image_base64: str = ""
    substituents: list[PeeledSubstituent] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    peel_parity_ok: bool = True
    peel_parity_mismatches: list[dict[str, Any]] = field(default_factory=list)
    label_side_relations_ok: bool = False
    label_side_relation_notes: list[str] = field(default_factory=list)


@dataclass
class BondMismatch:
    template_i: int
    template_j: int
    full_i: int
    full_j: int
    template_bond: str
    full_bond: str


@dataclass
class StereoMismatch:
    template_idx: int
    full_idx: int
    template_stereo: str
    full_stereo: str
    note: str


@dataclass
class AlignmentMethodSummary:
    method: str
    candidate_count: int
    bond_ok_count: int
    full_pass_count: int
    max_mapped_atoms: int = 0


@dataclass
class MatchCandidate:
    index: int
    source: str
    template_to_full: dict[str, int]
    anchors: list[dict[str, Any]]
    mapped_atoms: int
    bond_order_ok: bool
    bond_mismatch_count: int
    bond_mismatches: list[dict[str, Any]] = field(default_factory=list)
    stereo_ok: bool = True
    stereo_mismatch_count: int = 0
    stereo_mismatches: list[dict[str, Any]] = field(default_factory=list)
    peel_topology_ok: bool = True
    peel_topology_note: str = ""
    peel_topology_canonical_template: str = ""
    peel_topology_canonical_peeled: str = ""
    peel_topology_inchi_template: str = ""
    peel_topology_inchi_peeled: str = ""
    peel_topology_nonisomeric_match: bool = False
    peel_topology_stereo_missing_hint: bool = False
    peel_stereo_ok: bool = True
    peel_stereo_mismatch_count: int = 0
    peel_stereo_mismatches: list[dict[str, Any]] = field(default_factory=list)
    peel_chiral_centers: list[dict[str, Any]] = field(default_factory=list)
    peel_parity_ok: bool = True
    peel_parity_mismatches: list[dict[str, Any]] = field(default_factory=list)
    label_side_relations_ok: bool = False
    label_side_relation_notes: list[str] = field(default_factory=list)
    highlight_image_base64: str = ""
    labeled_image_base64: str = ""
    peel_scaffold_smiles: str = ""
    peel_scaffold_image_base64: str = ""
    peeled_substituents: list[dict[str, Any]] = field(default_factory=list)
    roundtrip_ok: bool = False
    roundtrip_note: str = ""
    assembled_smiles: str = ""
    assembled_inchi_key: str = ""
    target_inchi_key: str = ""
    assembled_image_base64: str = ""
    assembly_messages: list[str] = field(default_factory=list)
    assembly_fragment_lines: list[str] = field(default_factory=list)
    library_ok: bool | None = None
    library_notes: list[str] = field(default_factory=list)
    virtual_arc_ok: bool = True
    virtual_arc_note: str = ""
    virtual_arc_checks: list[dict[str, Any]] = field(default_factory=list)
    duplicate_label_peel_warn: bool = False
    duplicate_label_peel_groups: list[dict[str, Any]] = field(default_factory=list)
    duplicate_label_peel_note: str = ""
    also_sources: list[str] = field(default_factory=list)
    merged_map_count: int = 1


@dataclass
class Step0MapResult:
    ok: bool
    match_count: int
    template_smiles: str
    template_heavy_smiles: str
    target_smiles: str
    template_heavy_atoms: int
    target_heavy_atoms: int
    matches: list[MatchCandidate] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    template_image_base64: str = ""
    target_image_base64: str = ""
    highlight_image_base64: str = ""
    template_chiral_centers: list[dict[str, Any]] = field(default_factory=list)
    target_chiral_centers: list[dict[str, Any]] = field(default_factory=list)
    alignment_summary: list[AlignmentMethodSummary] = field(default_factory=list)
    match_count_raw: int = 0
    match_count_index: int = 0


@dataclass
class ResolvedRepeatStep0Candidate:
    """One abstract-repeat resolution and its ordinary Step0 result."""

    template_esmiles: str
    repeat_assignments: list[dict[str, Any]]
    result: Step0MapResult


@dataclass
class ResolvedRepeatStep0Result:
    """Target-driven concrete alternatives for an abstract-repeat template."""

    ok: bool
    candidates: list[ResolvedRepeatStep0Candidate] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
