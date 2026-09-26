"""Import this FIRST in any entry point: `import bootstrap`. Sets CHEM_REPO + sys.path for all
bundled libs (agent, engine, skill, tools_product, molparser_utils, fhr, MarkushMapMulti) so the
release runs from any checkout location with no install step needed for the internal packages."""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
os.environ.setdefault("CHEM_REPO", str(ROOT))
for p in ("", "agent", "skills/chem_extract/engine", "skills/chem_extract_skills", "tools_product", "scripts"):
    d = str(ROOT / p) if p else str(ROOT)
    if d not in sys.path:
        sys.path.insert(0, d)
