"""On-demand PDF page rendering for chem_reviewer.

We don't preprocess pages. For ``GET /api/paper/<slug>/page/<n>`` we render
the source PDF page on the fly (PyMuPDF, 200 DPI) and cache to
``PDF/<slug>/.cache/page_NN.png`` so subsequent hits skip the re-render.

The cache directory is paper-local and ignorable; if the paper has no
PDF on disk we just 404.
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_DPI = 200
CACHE_DIR_NAME = ".cache"


def cache_dir(paper_dir: Path) -> Path:
    return paper_dir / CACHE_DIR_NAME


def page_cache_path(paper_dir: Path, page_1based: int, dpi: int = DEFAULT_DPI) -> Path:
    return cache_dir(paper_dir) / f"page_{page_1based:02d}_dpi{dpi}.png"


def render_pdf_page(
    pdf_path: Path,
    page_1based: int,
    out_path: Path,
    dpi: int = DEFAULT_DPI,
) -> tuple[int, int]:
    """Render one PDF page → PNG. Returns (width_px, height_px)."""
    import fitz  # PyMuPDF, lazy import so server startup doesn't fail if missing

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        if page_1based < 1 or page_1based > len(doc):
            raise IndexError(
                f"page {page_1based} out of range (PDF has {len(doc)} pages)"
            )
        page = doc[page_1based - 1]
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(str(out_path))
        return pix.width, pix.height
    finally:
        doc.close()


def get_or_render_page(
    paper_dir: Path,
    pdf_path: Path,
    page_1based: int,
    dpi: int = DEFAULT_DPI,
) -> Path | None:
    """Return cached page PNG path; render lazily if missing.

    Returns None if the source PDF doesn't exist (caller should 404).
    """
    if not pdf_path.exists():
        return None
    cache_fp = page_cache_path(paper_dir, page_1based, dpi=dpi)
    if cache_fp.exists():
        return cache_fp
    try:
        render_pdf_page(pdf_path, page_1based, cache_fp, dpi=dpi)
    except Exception:
        return None
    return cache_fp


def page_count(pdf_path: Path) -> int:
    """Return total page count of a PDF; 0 on error."""
    if not pdf_path.exists():
        return 0
    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0
