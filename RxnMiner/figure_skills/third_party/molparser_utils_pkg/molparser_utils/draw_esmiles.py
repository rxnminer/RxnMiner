import io
import math
import random
import re
import cairosvg
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from PIL import Image
from pydantic import BaseModel, Field
from rdkit import Chem, RDLogger
from rdkit.Chem.Draw import rdMolDraw2D
from rdkit.Chem.rdchem import Mol
from rdkit.Geometry import Point2D

# Import shared classes from translator module
from .translator import (
    TextType,
    Index,
    AtomIndex,
    RingIndex,
    Tokens,
    Patterns,
    GroupDesc,
    LONG_CHAR_ELEMENTS,
)


class VisualConfig(BaseModel):
    """Visual configuration settings for molecule drawing."""

    padding: float = Field(default=0.1, ge=0.0, le=1.0, description="Canvas padding")
    additionalAtomLabelPadding: float = Field(
        default=0.05, ge=0.0, le=0.5, description="Additional padding for atom labels"
    )
    fixedFontSize: int = Field(default=10, ge=1, le=100, description="Fixed font size for labels")
    bondLineWidth: int = Field(default=1, ge=1, le=10, description="Bond line width")
    multipleBondOffset: float = Field(default=0.1, ge=0.01, le=1.0, description="Multiple bond offset")
    dummiesAreAttachments: bool = Field(default=False, description="Treat dummy atoms as attachments")
    addAtomIndices: Union[bool, float] = Field(
        default=False,
        description="Boolean or probability (0-1) to randomly add atom indices",
    )
    singleColourWedgeBonds: bool = Field(default=True, description="Use single color for wedge bonds")
    legendFontSize: int = Field(default=9, ge=1, le=50, description="Legend font size")


class StylingConfig(BaseModel):
    """Styling configuration settings for molecule drawing."""

    palette: Union[Literal["cdk", "bw"], float] = Field(
        default="cdk",
        description="Palette name or probability (0-1) of using black & white palette",
    )
    use_modern_symbols: bool = Field(default=True, description="Use modern R-group symbols")
    enhanced_contrast: bool = Field(default=True, description="Enhanced contrast for better visibility")
    highlight_atoms: Union[bool, float] = Field(
        default=False,
        description="Highlight special atoms (e.g., R-groups, dummies), or probability (0-1)",
    )
    highlight_bonds: Union[bool, float] = Field(
        default=False,
        description="Highlight bonds connected to special atoms, or probability (0-1)",
    )
    custom_colors: Union[bool, float] = Field(
        default=False,
        description="Enable random custom highlight colors, or probability (0-1)",
    )
    bold_r_groups: bool = Field(default=True, description="Bold R-group labels")
    ring_connector_style: Literal["dashed", "solid"] = Field(
        default="solid", description="Ring connector line style"
    )
    ring_connector_extension: float = Field(
        default=0.8, ge=0.0, le=5.0, description="Extension length for ring connector arrow"
    )
    dummy_line_style: Literal["wavy", "dashed", "dotted", "solid"] = Field(
        default="wavy", description="Line style for dummy atom connections (wavy/dashed/dotted/solid)"
    )


class FeaturesConfig(BaseModel):
    """Feature configuration settings for molecule drawing."""

    dummy_atoms: bool = Field(default=True, description="Show dummy atoms")
    circled_r_groups: bool = Field(default=True, description="Draw circles around R-groups")
    ring_annotations: bool = Field(default=True, description="Show ring annotations")


class DrawingConfig(BaseModel):
    """Main configuration class for molecule drawing."""

    visual: VisualConfig = Field(default_factory=VisualConfig, description="Visual settings")
    styling: StylingConfig = Field(default_factory=StylingConfig, description="Styling settings")
    features: FeaturesConfig = Field(default_factory=FeaturesConfig, description="Feature settings")

    @classmethod
    def from_dict(cls, config_dict: Optional[Dict[str, Any]] = None) -> "DrawingConfig":
        """Create DrawingConfig from dictionary, merging with defaults."""
        if config_dict is None:
            return cls()

        # Start with default config
        merged_dict = {
            "visual": {},
            "styling": {},
            "features": {}
        }

        # Merge user config
        if config_dict:
            for section in ["visual", "styling", "features"]:
                if section in config_dict and isinstance(config_dict[section], dict):
                    merged_dict[section] = config_dict[section]

        return cls(**merged_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary format for backward compatibility."""
        return self.model_dump()


def _validate_config(config: Optional[Dict[str, Any]] = None) -> DrawingConfig:
    """Validate and create configuration using Pydantic models.

    Args:
        config: User configuration dictionary

    Returns:
        Validated DrawingConfig instance

    Raises:
        ValidationError: If configuration is invalid
    """
    return DrawingConfig.from_dict(config)


def _resolve_probability(value: Union[bool, float]) -> bool:
    """Normalize bool/float to bool by sampling if necessary."""
    if isinstance(value, bool):
        return value
    prob = min(max(value, 0.0), 1.0)
    return random.random() < prob


def _random_color() -> Tuple[float, float, float]:
    """Generate a random RGB color tuple."""
    return random.random() / 2 + 0.5, random.random() / 2 + 0.5, random.random() / 2 + 0.5


def _update_bounds(
    bounds: Optional[List[float]], x: float, y: float
) -> List[float]:
    if bounds is None:
        return [x, y, x, y]
    bounds[0] = min(bounds[0], x)
    bounds[1] = min(bounds[1], y)
    bounds[2] = max(bounds[2], x)
    bounds[3] = max(bounds[3], y)
    return bounds


def _estimate_text_bounds(
    label_text: str, x: float, y: float, font_px: float, margin: float
) -> Tuple[float, float, float, float]:
    # Strip simple SVG tags like <sub>...</sub> for length estimation.
    visible = re.sub(r"<[^>]+>", "", label_text)
    text_len = max(len(visible), 1)
    text_w = font_px * 0.6 * text_len + 2 * margin
    text_h = font_px + 2 * margin
    return x - text_w, y - text_h, x + text_w, y + text_h


def _format_symbol_with_subscripts(symbol: str) -> str:
    # Convert trailing digits in plain formulas (e.g., CH3, C2H5, SO4) to subscripts.
    # Only apply to raw symbol strings; do not touch existing HTML tags.
    if "<" in symbol and ">" in symbol:
        return symbol
    return re.sub(r"(\d+)", r"<sub>\1</sub>", symbol)


def _expand_svg_viewbox(svg_text: str, bounds: List[float], pad: float) -> str:
    try:
        root = ET.fromstring(svg_text)
    except Exception:
        return svg_text

    view_box = root.get("viewBox")
    if view_box:
        parts = view_box.split()
        if len(parts) == 4:
            try:
                vb_x, vb_y, vb_w, vb_h = map(float, parts)
            except Exception:
                return svg_text
        else:
            return svg_text
    else:
        width_attr = root.get("width", "")
        height_attr = root.get("height", "")
        width_match = re.search(r"[\d.]+", width_attr or "")
        height_match = re.search(r"[\d.]+", height_attr or "")
        if not width_match or not height_match:
            return svg_text
        vb_x, vb_y = 0.0, 0.0
        vb_w = float(width_match.group(0))
        vb_h = float(height_match.group(0))

    min_x, min_y, max_x, max_y = bounds
    new_min_x = min(vb_x, min_x - pad)
    new_min_y = min(vb_y, min_y - pad)
    new_max_x = max(vb_x + vb_w, max_x + pad)
    new_max_y = max(vb_y + vb_h, max_y + pad)
    new_w = new_max_x - new_min_x
    new_h = new_max_y - new_min_y
    new_vb = f"{new_min_x} {new_min_y} {new_w} {new_h}"
    root.set("viewBox", new_vb)
    root.set("overflow", "visible")

    # Expand white background rect if present.
    for child in root.iter():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag != "rect":
            continue
        style = child.get("style", "")
        if "fill:#FFFFFF" in style or "fill: #FFFFFF" in style:
            child.set("x", str(new_min_x))
            child.set("y", str(new_min_y))
            child.set("width", str(new_w))
            child.set("height", str(new_h))
            break

    # Expand clipPath rect if present.
    for clip in root.iter():
        tag = clip.tag.rsplit("}", 1)[-1]
        if tag != "clipPath":
            continue
        for rect in list(clip):
            rect_tag = rect.tag.rsplit("}", 1)[-1]
            if rect_tag == "rect":
                rect.set("x", str(new_min_x))
                rect.set("y", str(new_min_y))
                rect.set("width", str(new_w))
                rect.set("height", str(new_h))
    return ET.tostring(root, encoding="unicode")


# Override Patterns for drawing-specific regex (more lenient parsing)
class DrawingPatterns:
    """Extended Patterns class with more lenient regex for drawing purposes."""
    long_char_elements_pattern = Patterns.long_char_elements_pattern
    grp_content = re.compile(
        rf"(?P<{TextType.SYMBOL.value}>[^\[\?\'\"]*?)"  # Match everything before first bracket, ?, quote (non-greedy)
        + rf"(?P<{TextType.SCRIPT.value}>(\[\S+\])?)"  # Match one script part [xxx]
        + rf"(?P<{TextType.PRIME.value}>[\'\"]?)"
        + rf"(?P<{TextType.MULTIPLE.value}>(\?([a-z]|\d{{1}}|\d-\d)$)?)"
    )
    grp_pattern = re.compile(
        rf"({Tokens.atom_start}|{Tokens.circ_start}|{Tokens.ring_start}|{Tokens.ring_start}{Tokens.circ_start})"
        + r"(\d+:\S*?)"
        + rf"({Tokens.atom_end}|{Tokens.circ_end}|{Tokens.ring_end})"
    )
    trail_pattern = Patterns.trail_pattern


class DrawingTranslator:
    """A helper class for tokenizing, parsing, and reconstruction for drawing purposes.
    
    This class extends the base Translator functionality with drawing-specific features
    such as stereochemistry preservation and more lenient parsing.
    """

    @classmethod
    def _preserve_stereochemistry(cls, original_smiles: str, mol: Mol) -> None:
        """Extract and preserve stereochemistry information from original SMILES.

        This method uses RDKit's native SMILES parsing to correctly handle stereochemistry,
        avoiding manual parsing that can fail with complex molecular structures.
        """
        try:
            # Parse the original SMILES with stereochemistry preserved
            stereo_mol = Chem.MolFromSmiles(original_smiles, sanitize=False)
            if stereo_mol is None:
                return

            # Ensure both molecules have the same number of atoms
            if stereo_mol.GetNumAtoms() != mol.GetNumAtoms():
                # If atom counts differ, fall back to RDKit's automatic handling
                # This can happen when the E-SMILES format modifies the molecule structure
                return

            # Copy stereochemistry information from the stereo-preserving molecule
            for i in range(mol.GetNumAtoms()):
                original_atom = stereo_mol.GetAtomWithIdx(i)
                target_atom = mol.GetAtomWithIdx(i)

                # Copy chiral tag if the target atom doesn't have stereochemistry set
                if (target_atom.GetChiralTag() == Chem.ChiralType.CHI_UNSPECIFIED and
                    original_atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED):
                    target_atom.SetChiralTag(original_atom.GetChiralTag())

                # Copy bond stereochemistry
                for bond in original_atom.GetBonds():
                    begin_idx = bond.GetBeginAtomIdx()
                    end_idx = bond.GetEndAtomIdx()

                    # Find corresponding bond in target molecule
                    target_bond = mol.GetBondBetweenAtoms(begin_idx, end_idx)
                    if (target_bond and
                        target_bond.GetStereo() == Chem.BondStereo.STEREONONE and
                        bond.GetStereo() != Chem.BondStereo.STEREONONE):
                        target_bond.SetStereo(bond.GetStereo())

        except Exception:
            # If stereochemistry preservation fails, continue without it
            # This ensures the molecule drawing still works even if stereo parsing fails
            pass

    @classmethod
    def parse_caption(
        cls, caption: str, return_mol: bool = False, error_msg: bool = False
    ) -> Optional[Tuple[Union[Mol, str], str, str]]:
        """Parse a complete molecule caption or prediction."""
        smi, *trailings = caption.split(Tokens.separator)
        if len(trailings) != 1:
            if error_msg:
                print(
                    f"{len(trailings)} `{Tokens.separator}` found in caption: {caption}"
                )
            return

        if error_msg:
            RDLogger.EnableLog("rdApp.*")
        mol = Chem.MolFromSmiles(smi)
        RDLogger.DisableLog("rdApp.*")
        if mol is None:
            if error_msg:
                print(f"Invalid SMILES: {smi}")
            return

        # Extract and preserve stereochemistry information from original SMILES
        cls._preserve_stereochemistry(smi, mol)

        # Split groups description and extension.
        groups, ext = cls.parse_trailing(trailings[0])

        if return_mol:
            return mol, groups, ext
        return smi, groups, ext

    @classmethod
    def parse_trailing(cls, trailing: str):
        """Parse the trailing part in caption into R group and extension."""
        matched = re.match(DrawingPatterns.trail_pattern, trailing)
        if matched is None:
            return "", ""
        content = matched.groupdict()
        return content.get("groups", ""), content.get("extension", "")

    @classmethod
    def parse_groups(cls, seq: str) -> List[GroupDesc]:
        """Parse R group texts from predictions."""
        if seq == "":
            return []
        descriptions = []
        for grp_start, grp_content, _ in re.findall(DrawingPatterns.grp_pattern, seq):
            parsed = cls.parse_group(grp_content)
            if parsed is None:
                continue
            idx, grp_text = parsed
            if grp_start == Tokens.atom_start:
                grp_desc = GroupDesc(id=AtomIndex(idx))
                if len(grp_text) == 0:
                    grp_desc.is_dummy = True
            elif grp_start == Tokens.circ_start:
                grp_desc = GroupDesc(id=AtomIndex(idx), is_circle=True)
            elif grp_start == f"{Tokens.ring_start}{Tokens.circ_start}":
                grp_desc = GroupDesc(id=RingIndex(idx, virtual=True))
            elif grp_start == Tokens.ring_start:
                grp_desc = GroupDesc(id=RingIndex(idx))
            else:
                continue
            symbol = grp_text.get(TextType.SYMBOL)
            if symbol:
                symbol = _format_symbol_with_subscripts(symbol)
            grp_desc.symbol = symbol
            grp_desc.script = grp_text.get(TextType.SCRIPT)
            grp_desc.prime = grp_text.get(TextType.PRIME)
            grp_desc.multiple = grp_text.get(TextType.MULTIPLE)
            descriptions.append(grp_desc)

        return descriptions

    @classmethod
    def parse_group(cls, group: str) -> Optional[Tuple[int, Dict[TextType, str]]]:
        """Get text of a R group."""
        items = group.split(":")
        if len(items) != 2:
            return
        idx, content = items
        if not idx.isdigit():
            return
        idx = int(idx)
        if content == Tokens.dummy or content == "":
            return idx, {}
        # Gotta make sure that not all text types are empty string.
        grp_text = cls.get_group_texts(content)
        if len(grp_text) == 0:
            return

        return idx, grp_text

    @classmethod
    def get_group_texts(cls, content: str) -> Dict[TextType, str]:
        """Helper for reorganizing texts in R group."""
        texts = {}
        
        # Find all bracket positions
        bracket_matches = list(re.finditer(r'\[([^\]]+)\]', content))
        
        if bracket_matches:
            # Symbol is everything before first bracket + everything after last bracket
            first_bracket_start = bracket_matches[0].start()
            last_bracket_end = bracket_matches[-1].end()
            symbol_before = content[:first_bracket_start]
            symbol_after = content[last_bracket_end:]
            symbol_part = symbol_before + symbol_after
            
            # Extract script: all bracket contents + text between brackets
            script_text = ''
            for i, match in enumerate(bracket_matches):
                script_text += match.group(1)  # Content inside bracket
                # Add text between this bracket and next bracket (if any)
                if i < len(bracket_matches) - 1:
                    next_start = bracket_matches[i + 1].start()
                    text_between = content[match.end():next_start]
                    script_text += text_between
            texts[TextType.SCRIPT] = script_text
        else:
            # No brackets, treat as symbol only
            symbol_part = content
        
        # Remove ?xxx suffix and quotes from symbol
        # Check for multiple first (it comes before prime)
        multiple_match = re.search(r'\?([a-z]|\d{1}|\d-\d)$', symbol_part)
        if multiple_match:
            texts[TextType.MULTIPLE] = multiple_match.group(1)
            symbol_part = re.sub(r'\?([a-z]|\d{1}|\d-\d)$', '', symbol_part)
        
        # Check for prime
        prime_match = re.search(r'[\'\"]$', symbol_part)
        if prime_match:
            texts[TextType.PRIME] = prime_match.group()
            symbol_part = symbol_part[:-1]
        
        if symbol_part:
            texts[TextType.SYMBOL] = symbol_part
        
        return texts

    @classmethod
    def reconstruct_mol(
        cls,
        mol: Union[Mol, str],
        groups: Optional[str] = None,
        write_to: Optional[str] = None,
        drawer_type: Literal["SVG", "PNG"] = "SVG",
        config: Optional[Union[Dict[str, Any], DrawingConfig]] = None,
    ) -> Optional[Union[str, bytes]]:
        """Reconstruct a molecule based on prediction caption and draw.

        Args:
            mol: Molecule object or SMILES string
            groups: Group descriptions for R-groups and rings
            write_to: File path to save the drawing (optional)
            drawer_type: Type of drawing output ("SVG" or "PNG")
            config: Comprehensive drawing configuration dictionary. Can include:
                - "visual": Basic visual settings (padding, font size, bond width, etc.)
                - "styling": Styling preferences (palette, modern symbols, ring connectors)
                - "features": Feature toggles (dummy atoms, circled groups, annotations)

        Example:
            config = {
                "styling": {
                    "palette": "cdk",
                    "ring_connector_style": "solid"
                },
                "features": {
                    "dummy_atoms": True
                }
            }
        """
        # Validate and create configuration
        if isinstance(config, DrawingConfig):
            drawing_config = config
        else:
            drawing_config = _validate_config(config)

        if drawer_type not in ("SVG", "PNG"):
            print(f"Invalid drawer type `{drawer_type}`")
            return
        if isinstance(mol, Mol):
            assert groups is not None, "Empty group information"
        elif isinstance(mol, str):
            assert groups is None, "Excessive group information"
            parsed = cls.parse_caption(caption=mol, return_mol=True)
            if parsed is None:
                return
            mol, groups, ext = parsed
        else:
            raise TypeError(f"Invalid argument type: `{mol.__class__.__name__}`")

        grp_descriptions = cls.parse_groups(groups)

        # First check if this molecule has circle.
        circ_indices = [
            int(desc.id)
            for desc in grp_descriptions
            if desc.is_circle and desc.symbol is not None
        ]

        ring_captions: List[str] = []
        ring_annotations: List[Tuple[int, str]] = []
        for desc in grp_descriptions:
            i = int(desc.id)
            label = None
            if isinstance(desc.id, AtomIndex):
                if not 0 <= i < mol.GetNumAtoms():
                    continue
                atom = mol.GetAtomWithIdx(i)
                if atom.GetSymbol() == "*":
                    if desc.is_dummy:  # `DummyGroup`
                        # For modern styling, we'll draw wavy lines instead of labels
                        label = "" if drawing_config.styling.use_modern_symbols else "dum"
                    elif desc.symbol is None:
                        continue
                    elif desc.is_circle:  # `CircledRGroup`
                        # Use regular label for circled R-groups (circle will be drawn separately)
                        label = desc.symbol if drawing_config.styling.use_modern_symbols else f"c{desc.symbol}"
                    else:
                        # Use more modern R-group notation
                        label = desc.symbol  # Keep original symbol for compatibility
                        if desc.script is not None:
                            label += f"<sub>{desc.script}</sub>"
                        if desc.prime is not None:
                            label += desc.prime
                        if desc.multiple is not None:  # `VariableGroup`
                            label = "(" + label + ")" + f"<sub>{desc.multiple}</sub>"
                elif atom.GetSymbol() in (
                    "C",
                    "O",
                ):  # `VariableGroup` on carbon or oxygen
                    if desc.symbol is None and desc.multiple is not None:
                        label = f"({atom.GetSymbol()})<sub>{desc.multiple}</sub>"
                if label is not None:
                    atom.SetProp("_displayLabel", label)
            elif isinstance(desc.id, RingIndex):  # Ring groups
                # Check if ring index is virtual.
                if (
                    (not desc.id.virtual)
                    and (not 0 <= i < mol.GetRingInfo().NumRings())
                    or (desc.id.virtual)
                    and (i not in circ_indices)
                    or desc.symbol is None
                ):
                    continue
                # Use more modern ring group notation
                label = desc.symbol  # Keep original symbol for compatibility
                if desc.script is not None:
                    label = label + (
                        f"<sub>{desc.script}</sub>"
                        if drawing_config.styling.use_modern_symbols
                        else f"[{desc.script}]"
                    )  # Use sub instead of brackets for modern styling
                if desc.prime is not None:
                    label = label + desc.prime
                if desc.multiple is not None:
                    label = (
                        "("
                        + label
                        + ")"
                        + (
                            f"<sub>{desc.multiple}</sub>"
                            if drawing_config.styling.use_modern_symbols
                            else desc.multiple
                        )
                    )

                # no caption since we display ring labels at the end of the dash line
                if desc.symbol is not None:
                    if desc.multiple:
                        ring_label_text = "("
                        ring_label_text += desc.symbol
                        if desc.script:
                            ring_label_text += "<sub>" + desc.script + "</sub>"
                        if desc.prime:
                            ring_label_text += desc.prime
                        ring_label_text += ")"
                        ring_label_text += "<sub>" + desc.multiple + "</sub>"
                    else:
                        ring_label_text = desc.symbol
                        if desc.script:
                            ring_label_text += "<sub>" + desc.script + "</sub>"
                        if desc.prime:
                            ring_label_text += desc.prime

                    ring_annotations.append((i, ring_label_text))

        # Refine atom coordinates.
        mol.RemoveAllConformers()
        params = Chem.rdCoordGen.CoordGenParams()
        params.minimizerPrecision = params.sketcherBestPrecision
        Chem.rdCoordGen.AddCoords(mol, params)  # conformer generated with ID=0
        Chem.rdDepictor.StraightenDepiction(mol, 0)  # might rotate a little

        # Store dummy atom information for wavy line drawing
        dummy_info = []
        if drawing_config.features.dummy_atoms:
            for desc in grp_descriptions:
                if isinstance(desc.id, AtomIndex) and desc.is_dummy:
                    atom_idx = int(desc.id)
                    if atom_idx < mol.GetNumAtoms():
                        atom = mol.GetAtomWithIdx(atom_idx)
                        # Find the connected atom (usually carbon)
                        connected_atoms = [
                            bond.GetOtherAtomIdx(atom_idx) for bond in atom.GetBonds()
                        ]
                        if connected_atoms:
                            connected_idx = connected_atoms[0]  # Take first connection
                            dummy_info.append((atom_idx, connected_idx))

        # Determine palette choice with probability support
        palette_setting = drawing_config.styling.palette
        if isinstance(palette_setting, str):
            use_bw_palette = palette_setting == "bw"
        else:
            bw_prob = min(max(palette_setting, 0.0), 1.0)
            use_bw_palette = random.random() < bw_prob

        # Prepare highlighting information for modern styling / configurable via StylingConfig
        highlight_atoms = []
        highlight_colors = {}
        highlight_bonds = []
        highlight_bond_colors = {}

        if drawing_config.styling.enhanced_contrast and not use_bw_palette:
            # 黑白模式下不添加highlight
            use_atom_hl = _resolve_probability(drawing_config.styling.highlight_atoms)
            use_bond_hl = _resolve_probability(drawing_config.styling.highlight_bonds)
            custom_colors_active = _resolve_probability(drawing_config.styling.custom_colors)
            if custom_colors_active:
                dummy_bond_color = _random_color()
                rgroup_bond_color = _random_color()
                rgroup_atom_color = _random_color()
            else:
                dummy_bond_color = (0.5, 0.7, 1.0)
                rgroup_bond_color = (0.5, 0.8, 0.5)
                rgroup_atom_color = (0.2, 0.7, 0.2)
            for desc in grp_descriptions:
                if isinstance(desc.id, AtomIndex):
                    atom_idx = int(desc.id)
                    if atom_idx < mol.GetNumAtoms():
                        atom = mol.GetAtomWithIdx(atom_idx)
                        if use_atom_hl and desc.symbol is not None and not desc.is_circle and not desc.is_dummy:
                            highlight_atoms.append(atom_idx)
                            highlight_colors[atom_idx] = rgroup_atom_color
                        if use_bond_hl:
                            for bond in atom.GetBonds():
                                other_idx = bond.GetOtherAtomIdx(atom_idx)
                                bond_idx = mol.GetBondBetweenAtoms(atom_idx, other_idx).GetIdx()
                                if bond_idx in highlight_bonds:
                                    continue
                                highlight_bonds.append(bond_idx)
                                if desc.is_dummy:
                                    highlight_bond_colors[bond_idx] = dummy_bond_color
                                elif desc.symbol is not None:
                                    highlight_bond_colors[bond_idx] = rgroup_bond_color
        if write_to is not None:
            drawer_type = "PNG"
        if drawer_type == "SVG":
            drawer = rdMolDraw2D.MolDraw2DSVG(width=-1, height=-1)
        else:
            drawer = rdMolDraw2D.MolDraw2DCairo(width=-1, height=-1)
        dopts = rdMolDraw2D.MolDrawOptions()
        if use_bw_palette:
            dopts.useBWAtomPalette()
        else:
            dopts.useCDKAtomPalette()
        visual_options = drawing_config.visual.model_dump()
        visual_options["addAtomIndices"] = _resolve_probability(visual_options.get("addAtomIndices", False))
        for k, v in visual_options.items():
            setattr(dopts, k, v)
        drawer.SetDrawOptions(dopts)
        rdMolDraw2D.PrepareAndDrawMolecule(
            drawer,
            mol,
            highlightAtoms=highlight_atoms,
            highlightAtomColors=highlight_colors,
            highlightBonds=highlight_bonds,
            highlightBondColors=highlight_bond_colors,
            legend=" | ".join(ring_captions),
        )
        if drawing_config.features.dummy_atoms and dummy_info and drawer_type == "SVG":
            conf = mol.GetConformer()
            for dummy_idx, connected_idx in dummy_info:
                try:
                    dummy_pos = conf.GetAtomPosition(dummy_idx)
                    connected_pos = conf.GetAtomPosition(connected_idx)
                    dx = connected_pos.x - dummy_pos.x
                    dy = connected_pos.y - dummy_pos.y
                    length = math.sqrt(dx * dx + dy * dy)
                    if length > 0:
                        dx /= length
                        dy /= length
                        perp_x = dy
                        perp_y = -dx
                        extension_length = 0.35
                        start_x = dummy_pos.x - perp_x * extension_length
                        start_y = dummy_pos.y - perp_y * extension_length
                        end_x = dummy_pos.x + perp_x * extension_length
                        end_y = dummy_pos.y + perp_y * extension_length
                        p1 = Point2D(start_x, start_y)
                        p2 = Point2D(end_x, end_y)
                        wavy_color = (0.0, 0.0, 0.0)
                        drawer.DrawWavyLine(p1, p2, wavy_color, wavy_color, nSegments=8, vertOffset=0.08)
                except Exception as e:
                    print(
                        f"Warning: Failed to draw wavy line for dummy atom {dummy_idx}: {e}"
                    )
        if drawing_config.features.circled_r_groups and drawer_type == "SVG":
            conf = mol.GetConformer()
            for desc in grp_descriptions:
                if isinstance(desc.id, AtomIndex) and desc.is_circle:
                    atom_idx = int(desc.id)
                    if atom_idx < mol.GetNumAtoms():
                        try:
                            atom_pos = conf.GetAtomPosition(atom_idx)
                            circle_radius = 0.5
                            p1 = Point2D(
                                atom_pos.x - circle_radius, atom_pos.y - circle_radius
                            )
                            p2 = Point2D(
                                atom_pos.x + circle_radius, atom_pos.y + circle_radius
                            )
                            drawer.SetColour((0, 0, 0))
                            drawer.SetFillPolys(False)
                            drawer.SetLineWidth(1)
                            drawer.DrawEllipse(p1, p2)
                        except Exception as e:
                            print(
                                f"Warning: Failed to draw circle for circled R-group at atom {atom_idx}: {e}"
                            )
        ring_bounds: Optional[List[float]] = None
        if (
            drawing_config.features.ring_annotations
            and ring_annotations
            and drawer_type == "SVG"
        ):
            conf = mol.GetConformer()
            ring_info = mol.GetRingInfo()
            atom_rings = ring_info.AtomRings()
            bond_rings = ring_info.BondRings()
            drawer.SetLineWidth(1)
            for ring_idx, label_text in ring_annotations:
                if not (0 <= ring_idx < len(atom_rings)):
                    continue
                ring_atoms = atom_rings[ring_idx]
                if not ring_atoms:
                    continue

                center_x = sum(conf.GetAtomPosition(idx).x for idx in ring_atoms) / len(
                    ring_atoms
                )
                center_y = sum(conf.GetAtomPosition(idx).y for idx in ring_atoms) / len(
                    ring_atoms
                )

                direction_x = direction_y = None
                ring_radius = None
                midpoint_distance = None
                bond_mid_data: List[Tuple[float, float, float]] = []
                if 0 <= ring_idx < len(bond_rings):
                    bond_ids = bond_rings[ring_idx]
                else:
                    bond_ids = []
                for bond_idx in bond_ids:
                    bond = mol.GetBondWithIdx(bond_idx)
                    pos1 = conf.GetAtomPosition(bond.GetBeginAtomIdx())
                    pos2 = conf.GetAtomPosition(bond.GetEndAtomIdx())
                    mid_x = (pos1.x + pos2.x) / 2.0
                    mid_y = (pos1.y + pos2.y) / 2.0
                    dx = mid_x - center_x
                    dy = mid_y - center_y
                    dist = math.hypot(dx, dy)
                    if dist > 1e-6:
                        bond_mid_data.append((dist, dx / dist, dy / dist))
                if bond_mid_data:
                    midpoint_distance, direction_x, direction_y = max(
                        bond_mid_data, key=lambda item: item[0]
                    )
                    ring_radius = midpoint_distance
                else:
                    for atom_idx in ring_atoms:
                        pos = conf.GetAtomPosition(atom_idx)
                        dx = pos.x - center_x
                        dy = pos.y - center_y
                        dist = math.hypot(dx, dy)
                        if dist > 1e-6:
                            direction_x = dx / dist
                            direction_y = dy / dist
                            ring_radius = dist
                            midpoint_distance = dist
                            break

                if ring_radius is None or direction_x is None:
                    continue

                if midpoint_distance is None:
                    midpoint_distance = ring_radius
                extension = drawing_config.styling.ring_connector_extension
                total_length = midpoint_distance + extension

                drawer.SetColour((0, 0, 0))
                if drawing_config.styling.ring_connector_style == "solid":
                    start_pt = Point2D(center_x, center_y)
                    end_pt = Point2D(
                        center_x + direction_x * total_length,
                        center_y + direction_y * total_length,
                    )
                    drawer.DrawLine(start_pt, end_pt)
                else:
                    dash_length = 0.15
                    gap_length = 0.1
                    offset = 0.0
                    while offset < total_length:
                        end_offset = min(offset + dash_length, total_length)
                        if end_offset > offset:
                            start_pt = Point2D(
                                center_x + direction_x * offset,
                                center_y + direction_y * offset,
                            )
                            end_pt = Point2D(
                                center_x + direction_x * end_offset,
                                center_y + direction_y * end_offset,
                            )
                            drawer.DrawLine(start_pt, end_pt)
                        offset = end_offset + gap_length

                label_offset = 0.25
                label_pt = Point2D(
                    center_x + direction_x * (total_length + label_offset),
                    center_y + direction_y * (total_length + label_offset),
                )
                drawer.SetColour((0, 0, 0))
                drawer.DrawString(label_text, label_pt, 1)

                draw_center = drawer.GetDrawCoords(Point2D(center_x, center_y))
                draw_label = drawer.GetDrawCoords(label_pt)
                ring_bounds = _update_bounds(ring_bounds, draw_center.x, draw_center.y)
                ring_bounds = _update_bounds(ring_bounds, draw_label.x, draw_label.y)
                if drawing_config.styling.ring_connector_style == "solid":
                    draw_end = drawer.GetDrawCoords(end_pt)
                    ring_bounds = _update_bounds(ring_bounds, draw_end.x, draw_end.y)
                else:
                    mol_end = Point2D(
                        center_x + direction_x * total_length,
                        center_y + direction_y * total_length,
                    )
                    draw_end = drawer.GetDrawCoords(mol_end)
                    ring_bounds = _update_bounds(ring_bounds, draw_end.x, draw_end.y)
                font_px = float(drawing_config.visual.fixedFontSize)
                min_x, min_y, max_x, max_y = _estimate_text_bounds(
                    label_text, draw_label.x, draw_label.y, font_px=font_px, margin=4.0
                )
                ring_bounds = _update_bounds(ring_bounds, min_x, min_y)
                ring_bounds = _update_bounds(ring_bounds, max_x, max_y)

        drawer.FinishDrawing()
        if write_to is not None:
            Path(write_to).parent.mkdir(parents=True, exist_ok=True)
            drawer.WriteDrawingText(write_to)
            return
        svg_text = drawer.GetDrawingText()
        if ring_bounds is not None:
            svg_text = _expand_svg_viewbox(svg_text, ring_bounds, pad=6.0)
        return svg_text


def draw(smi: str, config: Optional[Union[Dict[str, Any], DrawingConfig]] = None) -> str:
    """Draw molecule from SMILES or E-SMILES string and return SVG.

    Args:
        smi: Input string - either standard SMILES or E-SMILES with separator
        config: Comprehensive drawing configuration dictionary. Use preset functions like
            get_modern_config(), get_traditional_config(), etc., or create custom config.

    Returns:
        SVG string representation of the molecule

    Raises:
        RuntimeError: If molecule drawing fails

    Examples:
        # Using preset configuration
        svg = draw(smiles, config=get_modern_config())

        # Custom configuration
        config = {"styling": {"ring_connector_style": "solid"}}
        svg = draw(smiles, config=config)
    """
    if Tokens.separator in smi:
        svg = DrawingTranslator.reconstruct_mol(mol=smi, drawer_type="SVG", config=config)
    else:
        mol = Chem.MolFromSmiles(smi)
        svg = DrawingTranslator.reconstruct_mol(
            mol=mol, groups="", drawer_type="SVG", config=config
        )
    if svg is None:
        raise RuntimeError(f"Failed to draw molecule {smi}")
    return svg


def save_svg(smi: str, filepath: str, config: Optional[Union[Dict[str, Any], DrawingConfig]] = None) -> None:
    """Draw molecule from SMILES or E-SMILES string and save as SVG file.

    Args:
        smi: Input string - either standard SMILES or E-SMILES with separator
        filepath: Path where to save the SVG file
        config: Comprehensive drawing configuration dictionary (recommended)

    Raises:
        RuntimeError: If molecule drawing fails

    Examples:
        # Using preset configuration
        save_svg(smiles, "output.svg", config=get_high_contrast_config())

        # Custom configuration
        config = {"visual": {"fixedFontSize": 16}}
        save_svg(smiles, "output.svg", config=config)
    """
    svg_content = draw(smi, config=config)
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(svg_content)


def get_modern_config() -> DrawingConfig:
    """Get modern, vivid visualization configuration."""
    return DrawingConfig(
        styling=StylingConfig(
            palette="cdk",
            use_modern_symbols=True,
            enhanced_contrast=True,
            ring_connector_style="solid",
        ),
        features=FeaturesConfig(
            dummy_atoms=True,
            circled_r_groups=True,
            ring_annotations=True,
        ),
    )


def draw_esmiles(esmi: str, dpi: int = 300) -> Tuple[Image.Image, str]:
    """Draw E-SMILES string and return PIL Image and SVG string.
    
    Args:
        esmi: E-SMILES string
        dpi: DPI for rendering (default: 300)
        
    Returns:
        Tuple of (PIL Image object, SVG string)
    """
    config = get_modern_config()
    svg_content = draw(esmi, config=config)
    if svg_content is None:
        raise RuntimeError(f"Failed to draw molecule {esmi}")
    try:
        root = ET.fromstring(svg_content)
        width_attr = root.get('width', '400')
        height_attr = root.get('height', '400')
        
        width = float(re.sub(r'[^\d.]', '', width_attr)) if width_attr else 400
        height = float(re.sub(r'[^\d.]', '', height_attr)) if height_attr else 400
        
        scale = dpi / 96.0
        output_width = int(width * scale)
        output_height = int(height * scale)
        
        png_bytes = cairosvg.svg2png(
            bytestring=svg_content.encode('utf-8'),
            output_width=output_width,
            output_height=output_height
        )
        image = Image.open(io.BytesIO(png_bytes))
        return image, svg_content
    except ImportError:
        raise ImportError("cairosvg is required for SVG to image conversion. Install it with: pip install cairosvg")

if __name__ == "__main__":
    image, svg_content = draw_esmiles("*C(O)c1cc(C(=O)N(*)*)cc(-c2*ccc*2)c1<sep><a>0:CF3</a><a>9:R[3]</a><a>10:R[2]</a><a>14:X</a><a>18:Y</a><r>1:R[1]?1-3</r>", dpi=300)
    image.save("example.png")
    with open("example.svg", "w", encoding="utf-8") as f:
        f.write(svg_content)
