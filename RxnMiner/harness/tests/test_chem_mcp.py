"""chem_mcp -- the self-contained MCP stdio server that exposes the chemistry engine to any MCP host
(opencode etc.). Offline: exercises the JSON-RPC dispatch + the rdkit-only tools; the egress tools
(extract_reactions/ocsr_detect) are only smoke-checked for registration, not invoked."""
import json
import os
from pathlib import Path

# chem_mcp does os.environ.setdefault("FHR_REVERSE","1") at import (so the MCP server runs fhr by default).
# In the pytest process that would leak into OTHER tests (activating the fhr R-group path for tests that
# expect the base path). Restore FHR_REVERSE to its pre-import value so this module stays isolated.
_fhr_before = os.environ.get("FHR_REVERSE")
import chem_mcp as M  # noqa: E402
if _fhr_before is None:
    os.environ.pop("FHR_REVERSE", None)
else:
    os.environ["FHR_REVERSE"] = _fhr_before

REPO = Path(__file__).resolve().parent.parent
# repo layout: scripts/release_examples/expected/ ; release layout: examples/expected/ (build_release renames it)
FIXTURE_V2 = next((p for p in (REPO / "scripts" / "release_examples" / "expected" / "1.v2.json",
                               REPO / "examples" / "expected" / "1.v2.json") if p.exists()),
                  REPO / "examples" / "expected" / "1.v2.json")


def test_initialize_handshake():
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05"}})
    r = resp["result"]
    assert r["serverInfo"]["name"] == "chem-extract"
    assert r["protocolVersion"] == "2024-11-05"          # echoes client's version
    assert "tools" in r["capabilities"]


def test_notifications_get_no_reply():
    assert M.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_has_all_with_schema():
    resp = M.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert set(tools) == {"extract_reactions", "ocsr_detect", "detect", "assemble",
                          "verify_smiles", "render_reactions", "reverse_rgroup", "fill_rgroup",
                          "reverse_assemble"}
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object" and "description" in t


def test_verify_smiles_valid_and_invalid():
    ok = M.tool_verify_smiles("C[C@H](N)C(=O)O")
    assert ok["valid"] and ok["formula"] == "C3H7NO2" and ok["canonical_iso"] == "C[C@H](N)C(=O)O"
    assert "<svg" in ok["svg"]
    bad = M.tool_verify_smiles("C1CC((")
    assert bad["valid"] is False


def test_call_tool_wraps_text_content():
    resp = M.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "verify_smiles", "arguments": {"smiles": "CCO"}}})
    r = resp["result"]
    assert r["isError"] is False
    payload = json.loads(r["content"][0]["text"])
    assert payload["valid"] and payload["formula"] == "C2H6O"


def test_unknown_tool_is_error():
    r = M._call_tool("nope", {})
    assert r["isError"] is True and "unknown tool" in r["content"][0]["text"]


def test_unknown_method_returns_jsonrpc_error():
    resp = M.handle({"jsonrpc": "2.0", "id": 9, "method": "does/not/exist"})
    assert resp["error"]["code"] == -32601


def test_render_reactions_offline(tmp_path):
    if not FIXTURE_V2.exists():
        import pytest
        pytest.skip("v2 fixture missing")
    out = tmp_path / "view.html"
    res = M.tool_render_reactions(str(FIXTURE_V2), out_html=str(out))
    assert out.exists() and res["n_examples"] >= 1
    assert "<!doctype html>" in out.read_text().lower()


def test_assemble_decouples_plan_to_v2():
    """The `assemble` tool turns an understanding-friendly plan into strict V2, applying the read-first
    guards: a concrete cell (molparser_markush False) uses the OCSR read, and the printed number in
    product.identifier becomes the instance_id (not the bbox index)."""
    plan = {"selected_examples": [{
        "reactants": [{"smiles": "BrCc1ccccc1"}],
        "products": [{"smiles": "COc1ccccc1", "identifier": "39", "bbox_index": 34,
                      "molparser_smi": "CCOc1ccccc1", "molparser_markush": False}],
        "conditions": []}]}
    r = M.tool_assemble(plan)
    assert r["n_examples"] == 1
    e = r["v2"]["selected_examples"][0]
    assert e["instance_id"] == "39"                       # printed number, not bbox 34
    assert e["products"][0]["E-SMILES"] == "CCOc1ccccc1"  # trusted the concrete OCSR read
