"""Offline tests for genuine MolParser provenance in Chem Reviewer."""
import sys
from pathlib import Path


REVIEWER = Path(__file__).resolve().parents[1] / "chem_reviewer"
if str(REVIEWER) not in sys.path:
    sys.path.insert(0, str(REVIEWER))

from v2_loader import merge_raw_molparser_detections  # noqa: E402


def test_raw_molparser_fields_are_preserved_and_enriched():
    raw = [{
        "bbox_index": 5,
        "bbox": [10, 20, 30, 40],
        "smi": "*c1ccccc1",
        "caption": "*C1=CC=CC=C1<sep><a>0:X</a>",
        "esmi": "*c1ccccc1<sep><a>0:X</a>",
        "md5": "raw-md5",
        "score": 0.98,
        "markush": True,
    }]
    final_v2 = {
        "selected_examples": [{
            "instance_id": "3c",
            "reactants": [],
            "products": [{
                "identifier": "3c",
                "E-SMILES": "Fc1ccccc1",
                "_source": {
                    "type": "image_structure",
                    "bbox_index": 5,
                    "bbox": [10, 20, 30, 40],
                },
            }],
            "conditions": [],
        }],
    }

    merged = merge_raw_molparser_detections(raw, final_v2)

    assert len(merged) == 1
    detection = merged[0]
    assert detection["smi"] == "*c1ccccc1"
    assert detection["caption"] == "*C1=CC=CC=C1<sep><a>0:X</a>"
    assert detection["esmi"] == "*c1ccccc1<sep><a>0:X</a>"
    assert detection["md5"] == "raw-md5"
    assert detection["score"] == 0.98
    assert detection["markush"] is True
    assert detection["identifier"] == "3c"
    assert detection["role_hint"] == "product"
    assert detection["raw_available"] is True
    assert raw[0].get("identifier") is None


def test_dict_wrapped_raw_response_and_missing_items_are_tolerated():
    final_v2 = {"selected_examples": [], "reaction_templates": []}
    raw = {"detections": [{"smi": "CCO", "caption": "CCO"}, "bad"]}

    merged = merge_raw_molparser_detections(raw, final_v2)

    assert merged == [{
        "smi": "CCO",
        "caption": "CCO",
        "bbox_index": 0,
        "bbox": None,
        "identifier": "",
        "role_hint": "",
        "raw_available": True,
    }]
