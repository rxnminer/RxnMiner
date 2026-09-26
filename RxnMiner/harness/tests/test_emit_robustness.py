"""Offline tests for the emit robustness backstops (anti-clobber merge + yield-gap finish guard).

These protect the deterministic fields (printed yields, compound identifiers) that the main agent drops under
long-context load: a later emit must not erase a yield an earlier emit captured, and a V2 that grossly lacks
yields must be caught so the harness re-shows the figure instead of trusting the model's faded memory.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
import chem_harness as H  # noqa: E402


def _tmp(obj):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(obj, f); f.close()
    return f.name


def test_merge_restores_dropped_yields_and_identifiers():
    prior = {"selected_examples": [
        {"products": [{"identifier": "3", "E-SMILES": "CCO"}], "metrics": [{"role": "yield", "text": "73%", "of": "3"}]},
        {"products": [{"identifier": "3", "E-SMILES": "CCO"}], "metrics": [{"role": "yield", "text": "70%", "of": "3"}]}]}
    out = _tmp(prior)
    new = {"selected_examples": [
        {"products": [{"identifier": "3", "E-SMILES": "CCO"}], "metrics": []},          # dropped the yield
        {"products": [{"E-SMILES": "CCO"}], "metrics": None}]}                           # dropped yield + identifier
    m = H._merge_kept_fields(new, out)
    ys = [[x["text"] for x in (e.get("metrics") or [])] for e in m["selected_examples"]]
    assert ys == [["73%"], ["70%"]]                                                     # both restored
    assert m["selected_examples"][1]["products"][0]["identifier"] == "3"                # id restored (enables the yield key)


def test_merge_never_invents_and_leaves_dict_metrics_untouched():
    # no prior file -> passthrough unchanged
    plan = {"selected_examples": [{"products": [{"identifier": "x"}], "metrics": []}]}
    assert H._merge_kept_fields(plan, "/no/such/file.json") is plan
    # dict-form metrics are left exactly as-is (we only ever append to list-form)
    dm = {"selected_examples": [{"metrics": {"yield": "9%"}}]}
    assert H._merge_kept_fields(dm, "/no/such/file.json")["selected_examples"][0]["metrics"] == {"yield": "9%"}
    # a prior with NO yield for a product cannot fabricate one
    out = _tmp({"selected_examples": [{"products": [{"identifier": "3"}], "metrics": []}]})
    keep = {"selected_examples": [{"products": [{"identifier": "3"}], "metrics": []}]}
    assert H._merge_kept_fields(keep, out)["selected_examples"][0]["metrics"] == []


def test_merge_does_not_duplicate_when_a_yield_is_kept():
    # reps caught a 24-yields-for-12-examples bug: if the new emit KEPT a yield (even with a mismatched `of`),
    # the merge must NOT add the prior one on top.
    out = _tmp({"selected_examples": [{"products": [{"identifier": "3"}],
                "metrics": [{"role": "yield", "text": "73%", "of": "3"}]}]})
    new = {"selected_examples": [{"products": [{"identifier": "3"}],
           "metrics": [{"role": "yield", "text": "73%", "of": ""}]}]}            # kept, but `of` empty
    m = H._merge_kept_fields(new, out)
    assert len(m["selected_examples"][0]["metrics"]) == 1                         # no duplicate


