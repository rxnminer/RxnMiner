"""Callable tools for Opus-orchestrated extraction.

CLI form for Bash invocation (one-shot):
  python agent/tools.py molparser <image_path>
  python agent/tools.py ask_gemini <question> [--image PATH] [--pdf PATH]
  python agent/tools.py rdkit_canon <smi>
  python agent/tools.py paper_meta <paper_dir>       # parse the *.json metadata
  python agent/tools.py extract_caption <paper_text_path> <fig_label>
                                                    # e.g. "Scheme 2" or "Table 1"
  python agent/tools.py strip_stereo <smi> --center <idx> [--reason "..."] [--stereo-origin figure|text|substrate]
  python agent/tools.py strip_stereo <smi> --bond <idx>   [--reason "..."] [--stereo-origin ...]
  python agent/tools.py canonical_identity <smi>     # {smi_canon, inchi, identity_key, valid}
  python agent/tools.py detect_anchors <pdf> [--work-dir <dir>] [--max-side 1536]
                                                    # PDF -> anchors.json with
                                                    # accepted[] / rejected[]
  python agent/tools.py smoke                       # 5-step ~30s health check;
                                                    # exit 0 (ok or network-skip),
                                                    # 1 (import/RDKit/chem.ts wiring broken)

All tools print JSON to stdout. Errors go to stderr with non-zero exit.
The `smoke` subcommand is the lone exception: it prints plain text
`[ok]` / `[skip: <reason>]` / `[fail: <reason>]` lines for CI friendliness.
"""
import base64, datetime, hashlib, io, json, os, re, sys, tempfile, warnings
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

# Module-level binding of call_gemini so tests can patch the name at the
# import site (`agent.tools.call_gemini`). Per Plan 03-01 D-01: detect_anchors
# uses call_gemini directly (NOT the legacy ask_gemini shim) to keep retry
# semantics and the {answer, usage, used_model, ...} response shape explicit.
#
# The import is wrapped in try/except so `python agent/tools.py <cmd>` still
# runs standalone when `agent` is not on sys.path (the legacy CLI form used
# by the Phase 0 smoke test and by chem.ts spawn). When the import fails,
# `call_gemini` is set to None — detect_anchors imports it lazily inside
# the function body in that case (see implementation below).
try:
    from agent.llm_router import call_gemini  # type: ignore
except ImportError:
    call_gemini = None  # type: ignore[assignment]

REPO = Path(__file__).resolve().parent.parent
CREDS_PATH = Path(os.environ.get("VERTEX_CREDS") or (REPO / "agent" / ".creds" / "uniparser-new.json"))  # env override -> server's own chemagent creds

PROXY_KEYS = (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY", "socks_proxy", "SOCKS_PROXY",
)

# -------- detect_anchors constants (Plan 03-01 / D-01, D-02) ----------
# MolParser sanity threshold per Pitfall 2: any candidate whose best
# detection scores BELOW this value is dropped to rejected[]. The
# boundary value 0.3 itself belongs to ACCEPTED (strict `<` test).
_MIN_MOLPARSER_SCORE = 0.3

# Default longest-side cap for rendered PDF pages (Pitfall 3). Hard-coded
# DPI is forbidden — see _render_pdf_page implementation.
_DEFAULT_PDF_MAX_SIDE = 1536

# Vertex model name embedded in anchors.json for auditability. Bumping
# the primary model in llm_router does NOT auto-update this string; the
# anchors.json detector field is supposed to be a layout-detection
# audit anchor, not a live config mirror.
_DETECTOR_LABEL = "gemini-3.1-pro-preview"

# Gemini prompt for per-page anchor detection. Strict layout-only — no
# compound names, no SMILES, no chemistry-specific hints (D-01 prompt
# hygiene). Single AND double-column layouts supported; the model is
# instructed to return the WHOLE anchor as one bbox even if it spans
# columns (informs Pitfall 10 but does not solve it for v1).
PROMPT_ANCHOR_DETECT = """\
You are looking at a single rendered page from a scientific paper.

List ALL scheme / figure / table elements visible on this page. For each
one, return a JSON object with these keys:

  page             integer page number, set to 1 (caller will rewrite)
  bbox_pct         array of four floats [x0, y0, x1, y1] in 0.0..1.0,
                   expressed as fractions of the page width (x) and
                   height (y). The bbox covers the FULL anchor including
                   its caption line. If the anchor spans two text
                   columns, return ONE bbox that covers both columns.
  anchor_type      one of "scheme", "figure", or "table"
  caption          the full caption text as it appears on the page
                   (the line starting with "Scheme N.", "Figure N.",
                   or "Table N.")
  confidence       float 0.0..1.0, your confidence that this is a
                   real scheme/figure/table (not a body-text paragraph)
  n_likely_examples integer estimate of how many distinct compound
                   structures appear inside the bbox; 0 if it's a
                   pure-text table or a scheme of conditions

Return STRICT JSON of the form:

  {"anchors": [ <object>, <object>, ... ]}

If the page has no anchors, return `{"anchors": []}`.

Do NOT include any commentary, markdown code fences, or trailing text —
ONLY the JSON object. Both single-column and double-column page layouts
are supported.\
"""


# -------- Render one PDF page to PIL (Plan 03-01 / D-02) ----------
def _render_pdf_page(doc, page_idx: int,
                     target_max_side: int = _DEFAULT_PDF_MAX_SIDE):
    """Render ONE PDF page to a PIL image capped at `target_max_side` px.

    Per D-02 (Pitfall 3): NEVER hard-code 300 DPI. We render at a fixed
    2.0x zoom (yields ~144 DPI for ACS pages), then apply PIL
    `Image.thumbnail` as the final size cap so wide-aspect pages do not
    silently exceed `target_max_side` on the long axis.

    Parameters
    ----------
    doc : fitz.Document
        Already-opened PyMuPDF document. Caller owns close().
    page_idx : int
        0-based page index.
    target_max_side : int, default 1536
        Hard cap on `max(width_px, height_px)` of the returned image.

    Returns
    -------
    PIL.Image.Image
    """
    # Local import keeps top-level import time light for smoke / CLI.
    import fitz
    from PIL import Image

    page = doc.load_page(page_idx)
    # Matrix-based zoom (NOT `dpi=` keyword — that would tempt 300-DPI
    # regressions; see source-grep guard in test_pdf_detect.py).
    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    img.load()
    # Final size cap. Preserves aspect ratio; LANCZOS for downscale
    # quality. Pillow 11 — Image.Resampling enum is required.
    img.thumbnail((target_max_side, target_max_side), Image.Resampling.LANCZOS)
    return img


# -------- detect_anchors helpers ----------
def _strip_code_fence(text: str) -> str:
    """Strip a leading ```json / ``` and trailing ``` if Gemini wrapped output."""
    s = text.strip()
    if s.startswith("```"):
        # Drop the opening fence line entirely (covers ```json and bare ```).
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        else:
            s = s[3:]
    if s.endswith("```"):
        s = s[: -3]
    return s.strip()


def _parse_gemini_anchors(answer: str, page_num: int):
    """Parse one page's Gemini response into a list of anchor dicts.

    Raises ValueError on malformed JSON, embedding (truncated) offending
    text for log hygiene.
    """
    stripped = _strip_code_fence(answer)
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError) as e:
        snippet = (answer or "")[:200]
        raise ValueError(
            f"detect_anchors: malformed Gemini JSON on page {page_num}: "
            f"{snippet!r} ({e})"
        )
    anchors = payload.get("anchors") if isinstance(payload, dict) else None
    if anchors is None:
        snippet = (answer or "")[:200]
        raise ValueError(
            f"detect_anchors: missing `anchors` key on page {page_num}: "
            f"{snippet!r}"
        )
    return anchors


def _now_iso_z() -> str:
    """ISO-8601 UTC stamp with explicit Z suffix (mirrors _now_iso)."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic-write `payload` to `path` via tempfile + fsync + os.replace.

    Mirrors viewer/pricing.snapshot_pricing (lines 200-219). On failure
    the .tmp file is unlinked so no half-written state lingers.
    """
    work_dir = path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".anchors.", suffix=".tmp", dir=str(work_dir),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# -------- detect_anchors (Plan 03-01 / D-01) ----------
def detect_anchors(pdf_path: str,
                   work_dir: Optional[str] = None,
                   max_side: int = _DEFAULT_PDF_MAX_SIDE) -> dict:
    """Detect every scheme/figure/table anchor on a paper PDF.

    Pipeline (per CONTEXT D-01):
      1. Open `pdf_path` via PyMuPDF (`fitz`).
      2. For each page p (0-based), render via `_render_pdf_page` with
         `target_max_side=max_side`. Save to `<work_dir>/page_<p+1>.png`.
      3. Call `call_gemini(prompt=PROMPT_ANCHOR_DETECT, image_paths=[page_png])`.
      4. Parse Gemini's JSON into candidate anchors (bbox_pct + meta).
      5. For each candidate, convert bbox_pct → bbox_px on the rendered
         page, crop via `crop_bbox`, run MolParser sanity filter.
      6. Drop candidates with zero detections OR all-detections score<0.3
         to `rejected[]` with reason='no_extractable_structures'.
      7. Write `<work_dir>/anchors.json` atomically.

    Multi-column PDFs (Pitfall 10): the prompt instructs the model to
    treat a column-spanning anchor as a single bbox; for v1 this is
    sufficient for single-column ACS papers (paper01-style). Lower
    precision on multi-column layouts is an acceptable v1 limitation.

    Parameters
    ----------
    pdf_path : str
        Absolute or relative path to the source PDF.
    work_dir : str | None
        Where to write rendered PNGs + anchors.json. Defaults to a
        sibling directory of `pdf_path` named `<pdf_stem>_anchors/`.
    max_side : int, default 1536
        Longest-side cap for rendered pages (Pitfall 3).

    Returns
    -------
    dict
        The same payload written to `<work_dir>/anchors.json`. See D-01
        in CONTEXT for the full shape.
    """
    pdf = Path(pdf_path).resolve()
    if not pdf.is_file():
        raise FileNotFoundError(f"detect_anchors: pdf not found: {pdf}")

    if work_dir is None:
        work = pdf.parent / f"{pdf.stem}_anchors"
    else:
        work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    # Local imports keep top-level light.
    import fitz

    pdf_bytes = pdf.read_bytes()
    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    # Lazy-resolve call_gemini for the standalone-CLI path (where the
    # module-level binding may be None because `agent` isn't on sys.path).
    # Tests always patch `agent.tools.call_gemini`, so the patched value
    # takes precedence over this lookup.
    _call_gemini = call_gemini
    if _call_gemini is None:
        # Re-import via sys.path manipulation: add repo root so `agent.*`
        # resolves. Mirrors the pattern the smoke step uses.
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from agent.llm_router import call_gemini as _call_gemini  # type: ignore

    doc = fitz.open(str(pdf))
    try:
        page_count = doc.page_count

        accepted = []
        rejected = []
        anchor_id_counter = 0

        for p in range(page_count):
            page_num = p + 1  # 1-based for human-readable output
            img = _render_pdf_page(doc, p, target_max_side=max_side)
            page_png = work / f"page_{page_num}.png"
            img.save(str(page_png))
            rendered_w, rendered_h = img.size

            # Use the module-level binding so test patches at
            # `agent.tools.call_gemini` are honored. When that is None
            # the lazy resolution above has populated `_call_gemini`.
            cg = call_gemini if call_gemini is not None else _call_gemini
            resp = cg(
                prompt=PROMPT_ANCHOR_DETECT,
                image_paths=[str(page_png)],
            )
            answer = resp.get("answer", "") if isinstance(resp, dict) else ""
            candidates = _parse_gemini_anchors(answer, page_num)

            for cand in candidates:
                anchor_id_counter += 1
                bbox_pct = cand.get("bbox_pct") or [0.0, 0.0, 1.0, 1.0]
                # Clamp to [0,1] defensively before pixel conversion.
                bbox_pct = [max(0.0, min(1.0, float(v))) for v in bbox_pct]
                x0_px = int(round(bbox_pct[0] * rendered_w))
                y0_px = int(round(bbox_pct[1] * rendered_h))
                x1_px = int(round(bbox_pct[2] * rendered_w))
                y1_px = int(round(bbox_pct[3] * rendered_h))
                # Guarantee a non-degenerate crop.
                if x1_px <= x0_px: x1_px = min(rendered_w, x0_px + 1)
                if y1_px <= y0_px: y1_px = min(rendered_h, y0_px + 1)
                bbox_px = [x0_px, y0_px, x1_px, y1_px]

                base_entry = {
                    "anchor_id": anchor_id_counter,
                    "page": page_num,
                    "bbox_pct": bbox_pct,
                    "bbox_px": bbox_px,
                    "anchor_type": cand.get("anchor_type", "figure"),
                    "caption": cand.get("caption", ""),
                    "confidence": float(cand.get("confidence", 0.0)),
                    "n_likely_examples": int(
                        cand.get("n_likely_examples", 0) or 0
                    ),
                }

                # Sanity filter: crop + MolParser.
                crop = crop_bbox(str(page_png), bbox_px)
                dets = molparser(crop["out_path"])
                n_dets = len(dets)
                max_score = max(
                    (float(d.get("score", 0) or 0) for d in dets),
                    default=0.0,
                )

                if n_dets == 0 or max_score < _MIN_MOLPARSER_SCORE:
                    rejected.append({
                        **base_entry,
                        "molparser_n_detections": n_dets,
                        "molparser_max_score": max_score,
                        "reason": "no_extractable_structures",
                    })
                else:
                    accepted.append({
                        **base_entry,
                        "molparser_n_detections": n_dets,
                        "molparser_max_score": max_score,
                    })
    finally:
        doc.close()

    payload = {
        "pdf_path": str(pdf),
        "pdf_sha256": pdf_sha256,
        "detected_at": _now_iso_z(),
        "detector": _DETECTOR_LABEL,
        "page_count": page_count,
        "max_side": max_side,
        "accepted": accepted,
        "rejected": rejected,
    }
    _atomic_write_json(work / "anchors.json", payload)
    return payload


# -------- MolParser ----------
def molparser(image_path, timeout=180):
    """Recognize a source image using the configured UniParser account."""
    import sys
    source_root = str(Path(__file__).resolve().parents[2])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from figure_skills.tools.uniparser_client import recognize_molecules
    return recognize_molecules(image_path, timeout=timeout)


# -------- Gemini consultant ----------
def ask_gemini(question, image_path=None, pdf_path=None, model="gemini-2.5-pro"):
    """Ask Gemini multimodally. `image_path` can be a single path OR a
    list of paths — when multiple images are supplied Gemini sees them in
    order, which is how you do A/B compare (e.g., render_smi output vs
    source figure crop).

    Per AGENT-08 / D-04 this is now a thin shim over `agent.llm_router.call_gemini`
    so 429-class / preview-sunset failures retry with backoff + fall back
    to `gemini-2.5-pro` transparently. The returned dict preserves the
    existing `{answer, usage}` keys for chem.ts compatibility and adds
    `{used_model, used_fallback, fallback_reason, attempts}` (additive;
    chem.ts ignores unknown keys).
    """
    from agent.llm_router import call_gemini
    return call_gemini(
        question, image_paths=image_path, pdf_path=pdf_path, model=model,
    )


# -------- RDKit canonical ----------
def rdkit_canon(smi):
    from rdkit import Chem
    from rdkit import RDLogger
    RDLogger.DisableLog("rdApp.*")
    s = smi.split("<sep>")[0].strip()
    m = Chem.MolFromSmiles(s)
    if m is None:
        return {"input": smi, "canonical": None, "valid": False}
    return {
        "input": smi,
        "canonical": Chem.MolToSmiles(m),
        "canonical_no_stereo": Chem.MolToSmiles(m, isomericSmiles=False),
        "valid": True,
        "n_atoms": m.GetNumHeavyAtoms(),
    }


# -------- Per-center selective stereo strip (D-01, Pitfall 5) ----------
# These helpers implement the round-4 Stereo Discipline recovery path:
# erase chirality at EXACTLY ONE atom (or bond stereo at exactly ONE bond)
# via RDKit's per-element setters -- never via Chem.RemoveStereochemistry,
# which is the whole-molecule hammer the agent must never reach for.
#
# Refusal contract: when the upstream `_source.stereo_origin` is "text"
# (caption-derived (R)/(S)/(E)/(Z)) or "substrate" (defined by the
# starting material), stripping is forbidden -- the helper refuses and
# returns the SMILES unchanged. Only `figure`-origin stereo is strippable.
#
# Return shape: a flat dict the caller appends to `_strip_history` (a
# list) directly. The helper NEVER touches `_legacy_smiles` -- callers
# remain responsible for the append-only chain (Pitfall 6 prevention).
_STEREO_ORIGIN_REFUSE = {"text", "substrate"}


def _strip_stereo_preflight(smi, stereo_origin, reason):
    """Shared validate-and-refuse path for both center / bond helpers.

    Returns either (None, refuse_or_error_dict) when the operation must
    NOT proceed, or (mol, None) for the mutable RDKit Mol object.
    """
    from rdkit import Chem
    from rdkit import RDLogger
    RDLogger.DisableLog("rdApp.*")
    if stereo_origin in _STEREO_ORIGIN_REFUSE:
        return None, {
            "stripped": False,
            "refused": True,
            "reason": f"stereo_origin={stereo_origin} forbidden "
                      f"(only stereo_origin=figure is strippable)",
            "passthrough_reason": reason,
            "original_smi": smi,
            "new_smi": smi,
        }
    s = smi.split("<sep>")[0].strip() if isinstance(smi, str) else smi
    mol = Chem.MolFromSmiles(s) if isinstance(s, str) else None
    if mol is None:
        return None, {"ok": False, "error": "invalid SMILES", "original_smi": smi}
    return mol, None


def _now_iso():
    """ISO-8601 UTC timestamp with explicit Z suffix.

    Uses timezone-aware now (utcnow is deprecated in 3.12+).
    """
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def strip_stereo_at_center(smi, center_idx, *, reason="", stereo_origin="figure"):
    """Erase chirality at ONE atom via SetChiralTag(CHI_UNSPECIFIED).

    Per D-01 (Phase 1 Agent reliability hardening), this is the only
    sanctioned per-center strip API. The whole-molecule
    `Chem.RemoveStereochemistry` is explicitly forbidden by Pitfall 5.

    Parameters
    ----------
    smi : str
        Input SMILES (may include trailing `<sep>...` MolParser annotations
        which we strip the same way `rdkit_canon` does).
    center_idx : int
        Atom index of the chiral center to erase. Bounds-checked against
        mol.GetNumAtoms(). Use Chem.FindMolChiralCenters to discover.
    reason : str, kw-only
        Caller-supplied free-text rationale (e.g. "figure does not show
        wedge"); echoed back into the history record verbatim.
    stereo_origin : str, kw-only, default "figure"
        One of {"figure", "text", "substrate"}. "text" + "substrate" both
        refuse to strip and return the SMILES unchanged.

    Returns
    -------
    dict
        On strip success:
          {stripped: True, refused: False, when, center_idx,
           reason, original_smi, new_smi}
        On refuse:
          {stripped: False, refused: True, reason, original_smi, new_smi}
        On invalid SMILES or out-of-range index:
          {ok: False, error: str, ...}   (no exception ever raised)
    """
    from rdkit import Chem
    mol, blocker = _strip_stereo_preflight(smi, stereo_origin, reason)
    if blocker is not None:
        return blocker
    n_atoms = mol.GetNumAtoms()
    if not (0 <= center_idx < n_atoms):
        return {
            "ok": False,
            "error": f"center index {center_idx} out of range ({n_atoms} atoms)",
            "original_smi": smi,
        }
    atom = mol.GetAtomWithIdx(int(center_idx))
    atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
    new_smi = Chem.MolToSmiles(mol)
    return {
        "stripped": True,
        "refused": False,
        "when": _now_iso(),
        "center_idx": int(center_idx),
        "reason": reason,
        "original_smi": smi,
        "new_smi": new_smi,
    }


def strip_stereo_at_bond(smi, bond_idx, *, reason="", stereo_origin="figure"):
    """Erase double-bond stereo at ONE bond via SetStereo(STEREONONE).

    Per-bond analogue of `strip_stereo_at_center`. Same refusal contract
    and same history-record shape, except the index field is `bond_idx`
    instead of `center_idx`.
    """
    from rdkit import Chem
    mol, blocker = _strip_stereo_preflight(smi, stereo_origin, reason)
    if blocker is not None:
        return blocker
    n_bonds = mol.GetNumBonds()
    if not (0 <= bond_idx < n_bonds):
        return {
            "ok": False,
            "error": f"bond index {bond_idx} out of range ({n_bonds} bonds)",
            "original_smi": smi,
        }
    bond = mol.GetBondWithIdx(int(bond_idx))
    bond.SetStereo(Chem.BondStereo.STEREONONE)
    new_smi = Chem.MolToSmiles(mol)
    return {
        "stripped": True,
        "refused": False,
        "when": _now_iso(),
        "bond_idx": int(bond_idx),
        "reason": reason,
        "original_smi": smi,
        "new_smi": new_smi,
    }


# -------- InChI tiebreaker for identity equality (D-05, Pitfall 14) ----------
def canonical_identity(smi):
    """Return canonical SMILES + InChI + InChIKey as a stable identity key.

    `rdkit_canon` reports the RDKit-2025.09 canonical SMILES, but that
    string can drift across RDKit versions (Pitfall 14). For identity
    equality (e.g. attribution coverage checks at Phase-6 acceptance),
    use the InChIKey (`identity_key` field) as the tiebreaker -- it is
    stable across versions and write orders.

    This helper is intentionally observational. Invalid SMILES return
    `{valid: False}` with exit-0 semantics so the agent loop can call it
    speculatively without gating.

    Returns
    -------
    dict
        {input, smi_canon, inchi, identity_key, valid: True}
        OR {input, smi_canon: None, inchi: None, identity_key: None, valid: False}
    """
    from rdkit import Chem
    from rdkit import RDLogger
    from rdkit.Chem.inchi import MolToInchi, InchiToInchiKey
    RDLogger.DisableLog("rdApp.*")
    s = smi.split("<sep>")[0].strip() if isinstance(smi, str) else smi
    m = Chem.MolFromSmiles(s) if isinstance(s, str) else None
    if m is None:
        return {
            "input": smi,
            "smi_canon": None,
            "inchi": None,
            "identity_key": None,
            "valid": False,
        }
    inchi = MolToInchi(m)
    identity_key = InchiToInchiKey(inchi) if inchi else None
    return {
        "input": smi,
        "smi_canon": Chem.MolToSmiles(m),
        "inchi": inchi,
        "identity_key": identity_key,
        "valid": True,
    }


# -------- Paper metadata ----------
def paper_meta(paper_dir):
    """Return parsed metadata json for a paper directory, plus list of images."""
    d = Path(paper_dir)
    cands = list(d.glob("*.json"))
    if not cands:
        raise FileNotFoundError(f"no metadata .json in {d}")
    meta = json.loads(cands[0].read_text())
    txt_cands = list(d.glob("*_paper_text.txt"))
    return {
        "paper_dir": str(d),
        "pdf_name": meta.get("pdf_name"),
        "page_count": meta.get("page_count"),
        "screenshots": meta.get("screenshots", []),
        "paper_text_file": str(txt_cands[0]) if txt_cands else None,
    }


# -------- Caption extraction ----------
def extract_caption(paper_text_path, fig_label):
    """Find caption block in paper_text.txt for e.g. 'Scheme 2' or 'Table 1'.
    Returns the caption line + a few following lines until next blank-line block or another fig header."""
    txt = Path(paper_text_path).read_text()
    # match line starting with "<fig_label>." (e.g. "Scheme 2.")
    pat = re.compile(r"^" + re.escape(fig_label) + r"\.", re.MULTILINE)
    m = pat.search(txt)
    if not m:
        # also try without dot
        pat2 = re.compile(r"^" + re.escape(fig_label) + r"\b", re.MULTILINE)
        m = pat2.search(txt)
        if not m: return {"found": False, "label": fig_label}
    start = m.start()
    # End at next page marker or next fig header (Scheme/Table/Figure N.) or after ~600 chars
    rest = txt[start:start + 1200]
    nxt = re.search(r"^(=== page|(Scheme|Table|Figure) \d+\.)", rest[10:], re.MULTILINE)
    end_idx = (nxt.start() + 10) if nxt else min(len(rest), 800)
    caption = rest[:end_idx].strip()
    return {"found": True, "label": fig_label, "caption": caption}


# -------- Render SMILES to PNG ----------
def render_smi(smi, out_path=None, w=512, h=400, highlight_chirals=True):
    """RDKit-render a SMILES to a PNG file. Strips <sep>/Markush annotations
    first. Returns the absolute out_path. Used by the agent for visual
    cross-check (parsed SMILES vs source figure)."""
    import re as _re
    import tempfile
    from rdkit import Chem
    from rdkit.Chem.Draw import rdMolDraw2D
    s = smi.split("<sep>")[0]
    s = _re.sub(r"<[ar]>[^<]*</[ar]>", "", s).strip()
    if not s:
        raise ValueError(f"empty SMILES after stripping: {smi!r}")
    m = Chem.MolFromSmiles(s)
    if m is None:
        raise ValueError(f"invalid SMILES: {smi!r}")
    drawer = rdMolDraw2D.MolDraw2DCairo(int(w), int(h))
    opts = drawer.drawOptions()
    opts.clearBackground = True
    if highlight_chirals:
        # Mark every chiral center in red so the agent's eyes go to stereo first
        opts.addStereoAnnotation = True
    drawer.DrawMolecule(m)
    drawer.FinishDrawing()
    png_bytes = drawer.GetDrawingText()
    if out_path is None:
        out_path = tempfile.NamedTemporaryFile(
            prefix="render_smi_", suffix=".png", delete=False,
        ).name
    Path(out_path).write_bytes(png_bytes)
    return {"out_path": str(Path(out_path).resolve()), "bytes": len(png_bytes), "smi_canonical": Chem.MolToSmiles(m)}


# -------- Crop bbox from anchor PNG ----------
def crop_bbox(png_path, bbox, out_path=None, pad=12):
    """Crop a rectangular region from a PNG. bbox is [x0, y0, x1, y1] in pixel
    coords (MolParser convention). Returns the absolute out_path. Used by the
    agent to focus Gemini on one structure when verifying a stereo / regio
    assignment."""
    import tempfile
    from PIL import Image
    if isinstance(bbox, str):
        bbox = json.loads(bbox)
    bbox = list(map(int, bbox))
    if len(bbox) != 4:
        raise ValueError(f"bbox must be [x0,y0,x1,y1], got {bbox!r}")
    img = Image.open(png_path)
    W, H = img.size
    x0, y0, x1, y1 = bbox
    pad = int(pad)
    x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
    x1 = min(W, x1 + pad); y1 = min(H, y1 + pad)
    cropped = img.crop((x0, y0, x1, y1))
    if out_path is None:
        out_path = tempfile.NamedTemporaryFile(
            prefix="crop_bbox_", suffix=".png", delete=False,
        ).name
    cropped.save(out_path)
    return {
        "out_path": str(Path(out_path).resolve()),
        "size": list(cropped.size),
        "src_size": [W, H],
        "padded_bbox": [x0, y0, x1, y1],
    }


# -------- Smoke test ----------
SMOKE_TEST_SMI = "CC[C@H](C)Cl"  # chiral, also probes RDKit stereo handling


def smoke():
    """5-step ~30s health check that the chem-action codepath is alive.

    Steps (in order):
      1. Python imports                (HARD — fail → exit 1)
      2. MolParser :10619 ping         (SOFT — network fail → [skip])
      3. Vertex auth probe             (SOFT — network/creds fail → [skip])
      4. RDKit canonical round-trip    (HARD — fail → exit 1)
      5. chem.ts spawn wiring          (HARD if bun present, SOFT skip if not)

    Exit code:
      0 — all hard steps ok (soft steps may [skip]),
      1 — any hard step [fail].
    """
    hard_failed = False

    # ---- Step 1: Python imports (HARD) ----
    required = [
        "rdkit", "fitz", "google.genai",
        "vertexai.preview.generative_models",
        "PIL", "requests", "fastapi", "sqlite3",
    ]
    import_failures = []
    import importlib
    for mod in required:
        try:
            importlib.import_module(mod)
        except Exception as e:
            import_failures.append((mod, type(e).__name__))
    if import_failures:
        for mod, exc in import_failures:
            print(f"[fail: import {mod}: {exc}]")
        print("[fail: imports]")
        hard_failed = True
    else:
        print("[ok] imports")

    # Configuration checks never submit a billable recognition request.
    if os.environ.get("UNIPARSER_API_KEY"):
        print("[ok] uniparser_key_configured")
    else:
        print("[skip: set UNIPARSER_API_KEY for molecule recognition]")

    # ---- Step 3: Vertex auth probe (SOFT) ----
    # Never read the credential body into a string. Just hand the
    # file path to the SDK and call refresh(). Print exception class
    # only on failure (threat T-00-01).
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        creds = service_account.Credentials.from_service_account_file(
            str(CREDS_PATH),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(Request())
        print("[ok] vertex_auth")
    except Exception as e:
        print(f"[skip: vertex_auth {type(e).__name__}]")

    # ---- Step 4: RDKit canon round-trip (HARD) ----
    try:
        first = rdkit_canon(SMOKE_TEST_SMI)
        if not first.get("valid") or not first.get("canonical"):
            print(f"[fail: rdkit_canon first-pass invalid]")
            hard_failed = True
            rdkit_canonical = None
        else:
            second = rdkit_canon(first["canonical"])
            if (
                second.get("valid")
                and second.get("canonical") == first["canonical"]
            ):
                print(f"[ok] rdkit_canon ({first['canonical']})")
                rdkit_canonical = first["canonical"]
            else:
                print(
                    f"[fail: rdkit_canon non-idempotent "
                    f"first={first.get('canonical')!r} "
                    f"second={second.get('canonical')!r}]"
                )
                hard_failed = True
                rdkit_canonical = None
    except Exception as e:
        print(f"[fail: rdkit_canon {type(e).__name__}: {e}]")
        hard_failed = True
        rdkit_canonical = None

    # ---- Step 5: chem.ts spawn wiring ----
    # The chem.ts dispatcher is loaded by opencode at runtime (no
    # standalone CLI entry), but doCanon's pattern is: spawn
    # VENV_PY tools.py rdkit_canon <smi> from a bun TS context. We
    # replay that exact spawn from an inline bun -e script and
    # cross-check the canonical against step 4. This proves the
    # TS↔Python wiring (bun installed, can spawn Python, can
    # serialize JSON across the boundary) without touching chem.ts
    # itself.
    import shutil
    import subprocess
    bun = shutil.which("bun")
    if bun is None:
        print("[skip: chem_ts_spawn bun not on PATH]")
    elif rdkit_canonical is None:
        # Can't compare if step 4 itself failed; still attempt the
        # spawn to surface a wiring issue, but never call it [ok]
        # without a comparator.
        print("[skip: chem_ts_spawn rdkit_canon failed in step 4]")
    else:
        venv_py = str(REPO / "agent" / ".venv" / "bin" / "python")
        tools_py = str(REPO / "agent" / "tools.py")
        # Inline TS script: mirrors chem.ts doCanon's spawn pattern.
        ts_script = (
            "import { spawn } from 'child_process';"
            f"const c = spawn({venv_py!r}, [{tools_py!r}, 'rdkit_canon', {SMOKE_TEST_SMI!r}]);"
            "let out = ''; let err = '';"
            "c.stdout.on('data', d => out += d.toString());"
            "c.stderr.on('data', d => err += d.toString());"
            "c.on('close', code => {"
            "  process.stdout.write(JSON.stringify({code, out, err}));"
            "});"
        )
        try:
            r = subprocess.run(
                [bun, "-e", ts_script],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                stderr_tail = (r.stderr or "")[:80].replace("\n", " ")
                print(
                    f"[fail: chem_ts_spawn exit={r.returncode} "
                    f"stderr={stderr_tail!r}]"
                )
                hard_failed = True
            else:
                envelope = json.loads(r.stdout.strip())
                if envelope.get("code") != 0:
                    print(
                        f"[fail: chem_ts_spawn inner_exit={envelope.get('code')}]"
                    )
                    hard_failed = True
                else:
                    inner = json.loads(envelope.get("out", "{}"))
                    ts_canonical = inner.get("canonical")
                    if ts_canonical == rdkit_canonical:
                        print("[ok] chem_ts_spawn")
                    else:
                        print(
                            f"[fail: chem_ts_spawn wiring drift "
                            f"ts={ts_canonical!r} py={rdkit_canonical!r}]"
                        )
                        hard_failed = True
        except subprocess.TimeoutExpired:
            print("[fail: chem_ts_spawn timeout]")
            hard_failed = True
        except Exception as e:
            print(f"[fail: chem_ts_spawn {type(e).__name__}]")
            hard_failed = True

    return 1 if hard_failed else 0


# -------- CLI dispatch ----------
def _emit(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "molparser":
        _emit(molparser(sys.argv[2]))
    elif cmd == "ask_gemini":
        question = sys.argv[2]
        images = []
        pdf = None
        for i, a in enumerate(sys.argv[3:], 3):
            if a == "--image": images.append(sys.argv[i + 1])
            if a == "--pdf":   pdf = sys.argv[i + 1]
        img = images if len(images) > 1 else (images[0] if images else None)
        _emit(ask_gemini(question, img, pdf))
    elif cmd == "rdkit_canon":
        _emit(rdkit_canon(sys.argv[2]))
    elif cmd == "paper_meta":
        _emit(paper_meta(sys.argv[2]))
    elif cmd == "extract_caption":
        _emit(extract_caption(sys.argv[2], sys.argv[3]))
    elif cmd == "render_smi":
        smi = sys.argv[2]
        out_path = None
        for i, a in enumerate(sys.argv[3:], 3):
            if a == "--out": out_path = sys.argv[i + 1]
        _emit(render_smi(smi, out_path))
    elif cmd == "crop_bbox":
        png = sys.argv[2]
        bbox = sys.argv[3]
        out_path = None
        for i, a in enumerate(sys.argv[4:], 4):
            if a == "--out": out_path = sys.argv[i + 1]
        _emit(crop_bbox(png, bbox, out_path))
    elif cmd == "strip_stereo":
        # tools.py strip_stereo <smi> --center <idx>  [--reason ...] [--stereo-origin ...]
        # tools.py strip_stereo <smi> --bond   <idx>  [--reason ...] [--stereo-origin ...]
        if len(sys.argv) < 5:
            print("usage: strip_stereo <smi> (--center <idx> | --bond <idx>) "
                  "[--reason <str>] [--stereo-origin figure|text|substrate]")
            sys.exit(1)
        smi = sys.argv[2]
        center_idx = None
        bond_idx = None
        reason = ""
        stereo_origin = "figure"
        i = 3
        while i < len(sys.argv):
            a = sys.argv[i]
            if a == "--center":
                center_idx = int(sys.argv[i + 1]); i += 2
            elif a == "--bond":
                bond_idx = int(sys.argv[i + 1]); i += 2
            elif a == "--reason":
                reason = sys.argv[i + 1]; i += 2
            elif a == "--stereo-origin":
                stereo_origin = sys.argv[i + 1]; i += 2
            else:
                print(f"unknown flag: {a}"); sys.exit(1)
        if (center_idx is None) == (bond_idx is None):
            print("strip_stereo requires exactly one of --center or --bond")
            sys.exit(1)
        if center_idx is not None:
            result = strip_stereo_at_center(
                smi, center_idx, reason=reason, stereo_origin=stereo_origin,
            )
        else:
            result = strip_stereo_at_bond(
                smi, bond_idx, reason=reason, stereo_origin=stereo_origin,
            )
        _emit(result)
        # chem.ts detects failure via non-zero exit; observational success
        # (`stripped: True`) exits 0; refusal or error exits 1.
        if result.get("refused") or result.get("ok") is False:
            sys.exit(1)
    elif cmd == "canonical_identity":
        # tools.py canonical_identity <smi>
        # Observational: invalid SMILES still exits 0 with valid=False so
        # agent loops can call speculatively without gating.
        if len(sys.argv) < 3:
            print("usage: canonical_identity <smi>"); sys.exit(1)
        _emit(canonical_identity(sys.argv[2]))
    elif cmd == "detect_anchors":
        # tools.py detect_anchors <pdf> [--work-dir <dir>] [--max-side N]
        if len(sys.argv) < 3:
            print("usage: detect_anchors <pdf> [--work-dir <dir>] "
                  "[--max-side 1536]")
            sys.exit(1)
        pdf = sys.argv[2]
        work_dir = None
        max_side = _DEFAULT_PDF_MAX_SIDE
        i = 3
        while i < len(sys.argv):
            a = sys.argv[i]
            if a == "--work-dir":
                work_dir = sys.argv[i + 1]; i += 2
            elif a == "--max-side":
                max_side = int(sys.argv[i + 1]); i += 2
            else:
                print(f"unknown flag: {a}"); sys.exit(1)
        _emit(detect_anchors(pdf, work_dir=work_dir, max_side=max_side))
    elif cmd == "smoke":
        sys.exit(smoke())
    else:
        print(f"unknown command: {cmd}"); sys.exit(1)


if __name__ == "__main__":
    main()
