"""Chemistry algorithms: Step0 / peel / reverse assembly / forward assembly / rendering.

Facade names are loaded lazily so ``from chemistry.r_groups import …`` (used by
``esmiles``) does not pull ``reverse_assembly`` / ``esmiles`` during package init.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "assemble_fragments",
    "build_complete_molecule",
    "render_structure",
    "render_structure_base64",
    "reverse_assemble",
    "step0_scaffold_map",
    "step0_scaffold_map_resolving_repeats",
]

_LAZY = {
    "assemble_fragments": ("chemistry.assembly", "assemble_fragments"),
    "build_complete_molecule": ("chemistry.assembly", "build_complete_molecule"),
    "render_structure": ("chemistry.rendering", "render_structure"),
    "render_structure_base64": ("chemistry.rendering", "render_structure_base64"),
    "reverse_assemble": ("chemistry.reverse_assembly", "reverse_assemble"),
    "step0_scaffold_map": ("chemistry.decompose", "step0_scaffold_map"),
    "step0_scaffold_map_resolving_repeats": (
        "chemistry.decompose",
        "step0_scaffold_map_resolving_repeats",
    ),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr = target
    import importlib

    mod = importlib.import_module(mod_name)
    value = getattr(mod, attr)
    globals()[name] = value
    return value
