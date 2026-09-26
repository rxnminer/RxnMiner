"""Shared fixtures + path setup for the chemreaction component tests.

Run all:   agent/.venv/bin/python -m pytest tests/ -q
Run one:   agent/.venv/bin/python -m pytest tests/test_skill_tools.py -q

These tests exercise our components WITHOUT the network or Vertex creds:
  * skill tool.py verbs (canon/transform/reverse/fill/cond/abbrev)  -> RDKit + MarkushMap, local
  * emit engine (_mol_entry origin, emit-full -> V2)                 -> local
  * harness plumbing (mock model loop, overlay render, bbox enrich) -> local
  * scorer (norm_mol, smiles_accuracy, score_v2)                    -> local
  * product server pure logic + disk endpoints                      -> in-process, no HTTP
Tests that would need the network (opsin, molparser remote) or Vertex creds (a real Gemini
rollout) are intentionally omitted / marked, so the suite is fully offline and deterministic.
"""
import sys
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_VENV_PY = REPO / "agent" / ".venv" / "bin" / "python"     # this repo's interpreter (rdkit/vertexai/fastapi)
# A fresh clone (e.g. the release) has no agent/.venv -- fall back to whatever interpreter is running pytest,
# so `pytest tests/` is green anywhere the deps are installed instead of failing on a missing hardcoded path.
PY = _VENV_PY if _VENV_PY.exists() else Path(sys.executable)
SKILL = REPO / "skills" / "chem_extract_skills"            # the skill the harness points at
ENGINE_DIR = REPO / "skills" / "chem_extract"             # holds engine/v2.py (the live emit engine)

# make the in-repo modules importable by the test process
for p in (REPO, REPO / "agent", REPO / "service", ENGINE_DIR):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def skill(*args, timeout=120):
    """Invoke a skill tool.py verb in a subprocess; return (stdout_stripped, CompletedProcess)."""
    p = subprocess.run([str(PY), str(SKILL / "tool.py"), *map(str, args)],
                       capture_output=True, text=True, timeout=timeout)
    return (p.stdout or "").strip(), p


@pytest.fixture
def skill_run():
    return skill


@pytest.fixture
def sample_plan():
    """A minimal, valid extraction plan (one example, one reactant from a detection)."""
    return {
        "is_multistep": False,
        "_meta": {"task_id": "test", "paper_id": "p", "anchor_image": "0.png"},
        "selected_examples": [{
            "reactants": [{"smiles": "CSc1ccccc1", "identifier": "2a",
                           "bbox_index": 0, "molparser_md5": "deadbeef"}],
            "products": [{"smiles": "CS(=O)c1ccccc1", "identifier": "2b"}],
            "conditions": [[{"role": "solvent", "text": "EtOH"}]],
            "metrics": {},
        }],
        "reaction_templates": [],
        "reaction_graph": [],
    }


@pytest.fixture
def fake_paper(tmp_path):
    """A figure dir with a white 0.png + a 0_molparser.json (one detection), like a real job/work dir."""
    from PIL import Image
    Image.new("RGB", (300, 200), "white").save(tmp_path / "0.png")
    (tmp_path / "0_molparser.json").write_text(json.dumps([
        {"bbox_index": 0, "bbox": [10, 10, 100, 80], "smi": "CSc1ccccc1", "score": 1.0, "md5": "abc", "markush": False},
        {"bbox_index": 1, "bbox": [120, 90, 200, 170], "smi": "*c1ccccc1", "score": 0.4, "md5": "def", "markush": True},
    ]))
    return tmp_path
