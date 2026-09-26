"""Compatibility entry point for the UniParser molecule recognizer."""
try:
    from .uniparser_client import recognize_molecules
except ImportError:
    from uniparser_client import recognize_molecules


def molparser(image_path: str):
    """Recognize molecules through UniParser snip mode."""
    return recognize_molecules(image_path)
