"""Unified user-facing structure rendering.

Visible cards follow exactly one protocol:
``eSMILES → normalize → direct body Mol → 2D coords → wedge/dash → PNG``.
Step0/peel topology Mols are intentionally excluded: their placeholders and
coordinate reconstruction are for decomposition, not card depiction.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from rdkit import Chem
from rdkit.Chem import AllChem
from rdkit.Chem.Draw import rdMolDraw2D

from esmiles import (
    ESMILES_SEPARATOR,
    atom_id_by_position,
    load_smiles_body,
    normalize_fragment_esmiles,
    parse_acdv_tags,
    parse_ring_substituent_tags,
    split_esmiles,
)
from esmiles.ring_display import (
    attach_ring_position_variations,
    place_position_variation_dummies,
)
from esmiles.tilde_bonds import (
    get_tilde_pairs,
    paint_tilde_dashes_on_png,
    prepare_tilde_draw_mol,
    tilde_overlay_segments,
)

RenderKind = Literal["auto", "smiles", "esmiles", "mol"]
LabelStyle = Literal["atom_label", "atom_note", "none"]

DEFAULT_SIZE = (520, 360)


@dataclass
class RenderResult:
    image_base64: str = ""
    ok: bool = False
    normalized: str = ""
    messages: list[str] = field(default_factory=list)


@dataclass
class EsmilesDisplayModel:
    """The one direct Mol and label map used for an eSMILES card."""

    raw: str
    normalized: str
    mol: Chem.Mol
    atom_labels: dict[int, str] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    body_smiles: str = ""


def _labels_from_tags(info: str, atom_by_position: list[int]) -> dict[int, str]:
    from chemistry.r_groups import short_display_from_source

    labels: dict[int, str] = {}
    for tag in parse_acdv_tags(info):
        if tag.kind != "a":
            continue
        atom_idx = atom_id_by_position(atom_by_position, tag.position)
        if atom_idx >= 0:
            raw = (tag.raw_value or tag.value).strip()
            # Wire ``CH2?m`` / ``R[1]`` → on-structure ``(CH2)m`` / ``R1``.
            labels[atom_idx] = short_display_from_source(raw)
    return labels


def load_esmiles_display_model(
    value: str,
    *,
    expand_abbrevs: bool = True,
) -> EsmilesDisplayModel | None:
    """Normalize once and parse the eSMILES body directly for display."""
    raw = (value or "").strip()
    if not raw:
        return None
    messages: list[str] = []
    normalized = (
        normalize_fragment_esmiles(raw, messages) if expand_abbrevs else raw
    )
    body, info = split_esmiles(normalized)
    mol, atom_by_position, _backend = load_smiles_body(body, messages=messages)
    if mol is None:
        return None
    labels = _labels_from_tags(info, atom_by_position)
    ring_tags, _skipped, ring_warnings = parse_ring_substituent_tags(info)
    messages.extend(ring_warnings)
    if ring_tags:
        mol, ring_labels = attach_ring_position_variations(mol, ring_tags)
        labels.update(ring_labels)
    return EsmilesDisplayModel(
        raw=raw,
        normalized=normalized,
        mol=mol,
        atom_labels=labels,
        messages=messages,
        body_smiles=body,
    )


def _format_atom_note_label(text: str) -> str:
    """Viewer note style: ``R[1]`` → ``R1``; ``CH2?m`` → ``(CH2)m``."""
    from chemistry.r_groups import short_display_from_source

    return short_display_from_source(text)


def prepare_display_mol(
    mol: Chem.Mol,
    *,
    atom_labels: dict[int, str] | None = None,
    label_style: LabelStyle = "atom_label",
    smiles: str | None = None,
) -> Chem.Mol:
    """Generate coordinates and wedge/dash exactly once for every visible card.

    When the mol has aromatic-touching ``~`` bonds, those bonds are removed after
    layout so MolDraw2D can depict the aromatic ring normally; the removed bonds
    are re-drawn as dashed overlays in ``render_display_mol``.
    """
    display = Chem.Mol(mol)
    Chem.AssignStereochemistry(display, cleanIt=True, force=True)
    AllChem.Compute2DCoords(display)
    place_position_variation_dummies(display)
    AllChem.WedgeMolBonds(display, display.GetConformer())
    for idx, text in (atom_labels or {}).items():
        if not (0 <= idx < display.GetNumAtoms() and text):
            continue
        atom = display.GetAtomWithIdx(idx)
        # Position-variation label dummies: replace "*" with the R text.
        if atom.HasProp("_ringVarRole") and atom.GetProp("_ringVarRole") == "label":
            atom.SetProp("atomLabel", _format_atom_note_label(str(text)))
            continue
        if label_style == "atom_note":
            atom.SetProp("atomNote", _format_atom_note_label(str(text)))
        elif label_style == "atom_label":
            atom.SetProp("atomLabel", str(text))

    pairs = get_tilde_pairs(display, smiles)
    if pairs:
        display = prepare_tilde_draw_mol(display, pairs)
    return display


def render_display_mol(
    mol: Chem.Mol,
    *,
    size: tuple[int, int] = DEFAULT_SIZE,
    atom_labels: dict[int, str] | None = None,
    label_style: LabelStyle = "atom_label",
    stereo_annotation: bool = True,
    annotation_font_scale: float | None = None,
    smiles: str | None = None,
) -> str:
    """Draw a shared prepared display Mol without a second prepare pass."""
    display = prepare_display_mol(
        mol,
        atom_labels=atom_labels,
        label_style=label_style,
        smiles=smiles,
    )
    drawer = rdMolDraw2D.MolDraw2DCairo(*size)
    opts = drawer.drawOptions()
    opts.addAtomIndices = False
    opts.addStereoAnnotation = stereo_annotation
    if annotation_font_scale is not None:
        opts.annotationFontScale = annotation_font_scale
    drawer.DrawMolecule(display)
    segments = tilde_overlay_segments(drawer, display)
    drawer.FinishDrawing()
    png = drawer.GetDrawingText()
    if segments:
        png = paint_tilde_dashes_on_png(png, segments)
    return base64.b64encode(png).decode("ascii")


def render_structure(
    value: str | Chem.Mol | None,
    *,
    kind: RenderKind = "auto",
    label_style: LabelStyle = "atom_label",
    size: tuple[int, int] = DEFAULT_SIZE,
    prefer_indigo: bool = False,
    expand_abbrevs: bool = True,
    stereo_annotation: bool = True,
    annotation_font_scale: float | None = None,
) -> RenderResult:
    """Render a business-visible structure through the single display protocol."""
    messages: list[str] = []

    if kind == "mol" or isinstance(value, Chem.Mol):
        mol = value if isinstance(value, Chem.Mol) else None
        if mol is None:
            return RenderResult(messages=["kind=mol 需要 Chem.Mol"])
        b64 = render_display_mol(
            mol,
            size=size,
            label_style=label_style,
            stereo_annotation=stereo_annotation,
            annotation_font_scale=annotation_font_scale,
        )
        return RenderResult(image_base64=b64 or "", ok=bool(b64), messages=messages)

    raw = (str(value) if value is not None else "").strip()
    if not raw:
        return RenderResult()

    use_esmiles = kind == "esmiles" or (
        kind == "auto" and ESMILES_SEPARATOR in raw
    )

    if use_esmiles:
        model = load_esmiles_display_model(raw, expand_abbrevs=expand_abbrevs)
        if model is None:
            return RenderResult(
                normalized=raw,
                messages=["无法直接解析 eSMILES 展示体；不回退到 Step0/peel 模板 Mol。"],
            )
        labels = model.atom_labels if label_style != "none" else {}
        b64 = render_display_mol(
            model.mol,
            size=size,
            atom_labels=labels,
            label_style=label_style,
            stereo_annotation=stereo_annotation,
            annotation_font_scale=annotation_font_scale,
            smiles=model.body_smiles,
        )
        return RenderResult(
            image_base64=b64 or "",
            ok=bool(b64),
            normalized=model.normalized,
            messages=model.messages,
        )

    # Plain SMILES (or auto without <sep>)
    mol, _, _ = load_smiles_body(raw, messages=messages)
    b64 = (
        render_display_mol(
            mol,
            size=size,
            label_style=label_style,
            stereo_annotation=stereo_annotation,
            annotation_font_scale=annotation_font_scale,
            smiles=raw,
        )
        if mol is not None
        else ""
    )
    return RenderResult(
        image_base64=b64 or "",
        ok=bool(b64),
        normalized=raw,
        messages=[] if b64 else ["无法解析 SMILES。"],
    )


def render_structure_base64(
    value: str | Chem.Mol | None,
    **kwargs: Any,
) -> str:
    """Convenience: return PNG base64 only (empty string on failure)."""
    return render_structure(value, **kwargs).image_base64
