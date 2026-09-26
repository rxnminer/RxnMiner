from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
IMPORTER = ROOT / "harness/scripts/import_figure_result.py"


def test_imports_gt_free_figure_into_pretty_reviewer(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    Image.new("RGB", (32, 20), "white").save(image)
    prediction = {
        "reaction_templates": [],
        "selected_examples": [{
            "instance_id": "1",
            "reactants": [{"identifier": "A", "E-SMILES": "CCO"}],
            "products": [{"identifier": "B", "E-SMILES": "CC=O"}],
            "conditions": [[]],
        }],
    }
    pred = tmp_path / "pred.v2.json"
    pred.write_text(json.dumps(prediction, separators=(",", ":")), encoding="utf-8")
    molparser = tmp_path / "molparser.json"
    molparser.write_text("[]\n", encoding="utf-8")
    data_root = tmp_path / "data"

    result = subprocess.run([
        sys.executable, str(IMPORTER), "--image", str(image), "--pred", str(pred),
        "--molparser", str(molparser), "--data-root", str(data_root),
        "--collection", "test-figures", "--title", "One figure",
    ], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    imported = json.loads(result.stdout)
    job = data_root / "jobs" / imported["job_id"]
    reviewer = data_root / "reviewer" / imported["reviewer_slug"]
    assert (job / "result.json").read_bytes() == pred.read_bytes()
    assert (reviewer / "0_predicted_output.json").read_bytes() == pred.read_bytes()
    assert (reviewer / "0.png").is_file()
    assert (reviewer / "0_molparser.json").read_bytes() == molparser.read_bytes()
    assert json.loads((reviewer / "reactions.json").read_text())["title"] == "One figure"
    cards = [json.loads(line) for line in (
        data_root / "jobs/_collections/test-figures.jsonl"
    ).read_text().splitlines()]
    assert cards[-1]["reviewer_url"].endswith(f"#/{imported['reviewer_slug']}/0")


def test_rejects_reference_bearing_prediction(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    Image.new("RGB", (8, 8), "white").save(image)
    pred = tmp_path / "pred.v2.json"
    pred.write_text(json.dumps({
        "reaction_templates": [],
        "selected_examples": [{}],
        "gt_score": 1.0,
    }), encoding="utf-8")
    result = subprocess.run([
        sys.executable, str(IMPORTER), "--image", str(image), "--pred", str(pred),
        "--data-root", str(tmp_path / "data"),
    ], text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "forbidden reference/evaluation key" in result.stderr
