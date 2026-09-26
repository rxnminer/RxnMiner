#!/usr/bin/env python3
"""Offline end-to-end smoke test for the bundled chem-extract MCP server."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "run_chem_mcp.py"


def main() -> int:
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    request_id = 0

    def call(method: str, params: dict) -> dict:
        nonlocal request_id
        request_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("chem-extract exited without a JSON-RPC response")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(f"chem-extract error: {response['error']}")
        return response["result"]

    try:
        call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "rxn-skills-smoke", "version": "1"},
            },
        )
        listed = call("tools/list", {})
        names = [tool["name"] for tool in listed["tools"]]
        expected = {
            "ocsr_detect",
            "detect",
            "assemble",
            "verify_smiles",
            "render_reactions",
            "reverse_rgroup",
            "fill_rgroup",
            "reverse_assemble",
        }
        if set(names) != expected:
            raise RuntimeError(f"unexpected tool registry: {names}")

        verified = call(
            "tools/call",
            {"name": "verify_smiles", "arguments": {"smiles": "CCO"}},
        )
        verify_payload = json.loads(verified["content"][0]["text"])
        if not verify_payload.get("valid"):
            raise RuntimeError(f"SMILES verification failed: {verify_payload}")

        plan = {
            "selected_examples": [
                {
                    "identifier": "smoke",
                    "reactants": [{"identifier": "A", "smiles": "CCO"}],
                    "products": [{"identifier": "B", "smiles": "CC=O"}],
                    "conditions": [[{"role": "reagent", "text": "smoke test"}]],
                }
            ],
            "reaction_templates": [],
        }
        assembled = call(
            "tools/call",
            {"name": "assemble", "arguments": {"plan": plan}},
        )
        assemble_payload = json.loads(assembled["content"][0]["text"])
        if assemble_payload.get("n_examples") != 1:
            raise RuntimeError(f"V2 assembly failed: {assemble_payload}")

        print(f"OK: chem-extract MCP ready; {len(names)} tools; V2 assembly valid")
        return 0
    finally:
        process.stdin.close()
        process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
