import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from figure_skills.tools.uniparser_client import RecognitionError, normalize_detections, recognize_molecules


def response(*items):
    return {"status": "success", "pages_dict": [list(items)]}


class FakeClient:
    def __init__(self, result, submission=None):
        self.result = result
        self.submission = submission or {"status": "success", "token": "opaque-task"}
        self.calls = []

    def trigger_snip(self, **kwargs):
        self.calls.append(("snip", kwargs))
        return self.submission

    def get_result(self, token, **kwargs):
        self.calls.append(("result", {"token": token, **kwargs}))
        return self.result


class RecognitionTests(unittest.TestCase):
    def test_wire_strings_and_source_geometry_survive(self):
        raw = response({"type": "molecule", "bbox": [0.1, 0.2, 0.8, 0.9],
                        "esmi": "CC<sep>", "smi": "CC", "caption": "ethane", "conf": 0.97})
        record = normalize_detections(raw, (100, 200), "image-md5")[0]
        self.assertEqual(record["bbox"], [10, 40, 80, 180])
        self.assertEqual(record["esmi"], "CC<sep>")
        self.assertEqual(record["smi"], "CC")
        self.assertEqual(record["caption"], "CC<sep>")
        self.assertEqual(record["source_caption"], "ethane")
        self.assertEqual(record["md5"], "image-md5")
        self.assertEqual(raw["pages_dict"][0][0]["caption"], "ethane")

    def test_pixel_export_scales_to_source(self):
        raw = response({"type": "molecule", "bbox": {"x1": 10, "y1": 20, "x2": 90, "y2": 70},
                        "page_size": [100, 100], "esmi": "N"})
        self.assertEqual(normalize_detections(raw, (200, 300), "x")[0]["bbox"], [20, 60, 180, 210])

    def test_non_molecules_excluded_but_unreadable_molecule_retained(self):
        raw = response({"type": "paragraph", "text": "a caption"},
                       {"type": "molecule", "bbox": [0, 0, 1, 1], "esmi": ""})
        records = normalize_detections(raw, (200, 100), "x")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["caption"], "")

    def test_invalid_geometry_fails_instead_of_inventing_a_box(self):
        for box in ([1, 2, 3], [0.4, 0.3, 0.2, 0.1], [0, 0, float("nan"), 1], [-1, 0, 10, 10]):
            with self.subTest(box=box), self.assertRaises(RecognitionError):
                normalize_detections(response({"type": "molecule", "bbox": box}), (100, 100), "x")

    def test_snip_request_and_result_follow_one_token(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "figure.png"
            Image.new("RGB", (100, 200)).save(path)
            fake = FakeClient(response({"type": "molecule", "bbox": [0, 0, 1, 1], "esmi": "C<sep>"}))
            result = recognize_molecules(str(path), client=fake)
        self.assertEqual(result[0]["bbox"], [0, 0, 100, 200])
        self.assertEqual(fake.calls[0][0], "snip")
        self.assertEqual(fake.calls[0][1]["molecule"], 1)
        self.assertFalse(fake.calls[0][1]["padding_snip"])
        self.assertEqual(fake.calls[1][1]["token"], "opaque-task")
        self.assertTrue(fake.calls[1][1]["molecule_source"])

    def test_errors_do_not_echo_service_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "figure.png"
            Image.new("RGB", (10, 10)).save(path)
            for stage in ("submission", "result"):
                failure = {"status": "error", "http_status": 403, "description": "secret-provider-key"}
                fake = FakeClient(failure if stage == "result" else response(), failure if stage == "submission" else None)
                with self.subTest(stage=stage), self.assertRaises(RecognitionError) as raised:
                    recognize_molecules(str(path), client=fake)
                self.assertNotIn("secret-provider-key", str(raised.exception))

    def test_missing_key_is_actionable_and_makes_no_call(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "figure.png"
            Image.new("RGB", (10, 10)).save(path)
            with self.assertRaisesRegex(RecognitionError, "Set UNIPARSER_API_KEY"):
                recognize_molecules(str(path))

    def test_success_without_layout_is_not_treated_as_empty_recognition(self):
        with self.assertRaises(RecognitionError):
            normalize_detections({"status": "success"}, (10, 10), "x")


if __name__ == "__main__":
    unittest.main()
