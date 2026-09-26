"""Reusable eSMILES component: load, normalize, expand, encode.

Layers (call order for most pipelines)::

    normalize_fragment_esmiles / normalize_target_input   # expand + classify
    → load_esmiles / load_target_mol                     # string → Mol
    → chemistry (peel / assemble / match)
    → encode_substituent_esmiles                         # Mol → eSMILES write-back

Submodules:

- ``loader`` — DP-aligned parse (``<sep>`` + tags → RDKit Mol)
- ``ingress`` — concrete vs fragment classification and target normalize
- ``abbrev`` — abbreviation grafting (uses vendored ``translator/``); runs ``CH2?k`` first
- ``methylene`` — ``CH2?k`` thin-adapter expand; ``CH2?n`` keep
- ``encode`` — write ``<sep><a>`` back from dummies
- ``contract`` — attachment / virtual-arc contract for peel

Rendering belongs in a separate ``rendering`` layer (see ``7月11日待做.md``);
it should call this package rather than re-implement expand/load.
"""

from __future__ import annotations

from .abbrev import (
    expand_reactant_template_lines,
    expand_template_abbreviations,
    substitute_known_abbrev_slots,
    lookup_abbreviation_smiles,
)
from .text_markush import TextMarkushResult, compile_simple_text_markush
from .methylene import (
    MethyleneRepeatAssignment,
    MethyleneRepeatSpec,
    ResolvedMethyleneTemplate,
    abstract_methylene_bridge_specs,
    ch2_display_form,
    expand_numeric_ch2_in_esmiles,
    expand_numeric_ch2_in_esmiles_with_mol,
    is_abstract_ch2_label,
    is_ch2_repeat_label,
    parse_ch2_count,
    resolve_abstract_methylene_bridges,
)
from .contract import (
    AttachmentContract,
    RingSubstituentContract,
    VirtualArcCheckResult,
    VirtualArcContract,
    build_attachment_contract,
    contract_slot_from_label,
    parse_ring_substituent_tags,
    parse_virtual_arc_tag,
    pick_ring_substituent_anchor,
    ring_atom_sets,
    ring_atoms_by_esmiles_index,
    validate_virtual_arcs_for_alignment,
    virtual_arc_satisfied,
    virtual_arc_zone_template_atoms,
)
from .ring_display import (
    attach_ring_position_variations,
    place_position_variation_dummies,
)
from .encode import (
    encode_substituent_esmiles,
    position_for_atom_idx,
    remap_dummy_rlabels_after_roundtrip,
)
from .ingress import (
    NormalizedTarget,
    TargetMode,
    classify_target_esmiles,
    is_markush_slot_label,
    load_target_mol,
    normalize_fragment_esmiles,
    normalize_target_input,
)
from .loader import (
    ESMILES_DUMMY_TOKEN,
    ESMILES_SEPARATOR,
    EsmilesLoadResult,
    EsmilesSGroupTag,
    EsmilesTag,
    atom_id_by_position,
    build_atom_by_position,
    load_esmiles,
    load_esmiles_line,
    load_smiles_body,
    parse_acdv_tags,
    parse_fragment_line,
    resolve_anchor_dummy_indices,
    split_esmiles,
    validate_esmiles_line,
)

__all__ = [
    # constants
    "ESMILES_SEPARATOR",
    "ESMILES_DUMMY_TOKEN",
    # loader
    "EsmilesTag",
    "EsmilesSGroupTag",
    "EsmilesLoadResult",
    "split_esmiles",
    "load_esmiles",
    "load_esmiles_line",
    "load_smiles_body",
    "parse_fragment_line",
    "parse_acdv_tags",
    "build_atom_by_position",
    "atom_id_by_position",
    "resolve_anchor_dummy_indices",
    "validate_esmiles_line",
    # ingress
    "NormalizedTarget",
    "TargetMode",
    "is_markush_slot_label",
    "classify_target_esmiles",
    "normalize_fragment_esmiles",
    "normalize_target_input",
    "load_target_mol",
    # abbrev
    "expand_template_abbreviations",
    "expand_reactant_template_lines",
    "substitute_known_abbrev_slots",
    "lookup_abbreviation_smiles",
    "TextMarkushResult",
    "compile_simple_text_markush",
    # methylene CH2?k / CH2?n
    "MethyleneRepeatSpec",
    "MethyleneRepeatAssignment",
    "ResolvedMethyleneTemplate",
    "abstract_methylene_bridge_specs",
    "ch2_display_form",
    "parse_ch2_count",
    "is_ch2_repeat_label",
    "is_abstract_ch2_label",
    "expand_numeric_ch2_in_esmiles",
    "expand_numeric_ch2_in_esmiles_with_mol",
    "resolve_abstract_methylene_bridges",
    # encode
    "encode_substituent_esmiles",
    "position_for_atom_idx",
    "remap_dummy_rlabels_after_roundtrip",
    # contract
    "AttachmentContract",
    "RingSubstituentContract",
    "VirtualArcContract",
    "VirtualArcCheckResult",
    "build_attachment_contract",
    "contract_slot_from_label",
    "pick_ring_substituent_anchor",
    "parse_ring_substituent_tags",
    "parse_virtual_arc_tag",
    "ring_atom_sets",
    "ring_atoms_by_esmiles_index",
    "validate_virtual_arcs_for_alignment",
    "virtual_arc_satisfied",
    "virtual_arc_zone_template_atoms",
    # ring display (position variation)
    "attach_ring_position_variations",
    "place_position_variation_dummies",
]
