"""
MolParserUtils - Molecular Parser Utilities

A toolkit for parsing, refactoring, and visualizing molecular structures
represented in E-SMILES format (Extended SMILES with R-group annotations).
"""

from .translator import Translator # noqa

from .draw_esmiles import draw, draw_esmiles # noqa

__all__ = [
    # Core translation and refactoring
    "Translator"
    "draw",
    "draw_esmiles"
]

__version__ = "1.0.0"

