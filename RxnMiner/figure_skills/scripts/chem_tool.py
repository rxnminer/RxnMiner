#!/usr/bin/env python3
"""Stable command-line client for the bundled chem-extract MCP tools.

Examples:
    python figure_skills/scripts/chem_tool.py list
    python figure_skills/scripts/chem_tool.py call verify_smiles --args-json '{"smiles":"CCO"}'
    python figure_skills/scripts/chem_tool.py call detect --args-file /tmp/detect-args.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "run_chem_mcp.py"


class ChemToolClient:
    """Small synchronous JSON-RPC client over the MCP launcher's stdio."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.request_id = 0

    def __enter__(self) -> "ChemToolClient":
        self.process = subprocess.Popen(
            [sys.executable, str(LAUNCHER)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "chem-tool-cli", "version": "1.0.0"},
            },
        )
        self.notify("notifications/initialized", {})
        return self

    def __exit__(self, *_exc: object) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def request(self, method: str, params: dict[str, Any]) -> Any:
        process = self._running_process()
        assert process.stdin is not None
        assert process.stdout is not None
        self.request_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params,
        }
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            code = process.poll()
            raise RuntimeError(f"chem-extract MCP exited without a response (code={code})")
        response = json.loads(line)
        if response.get("id") != self.request_id:
            raise RuntimeError(
                f"unexpected MCP response id: {response.get('id')!r}; "
                f"expected {self.request_id}"
            )
        if "error" in response:
            error = response["error"]
            raise RuntimeError(error.get("message") or json.dumps(error))
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        process = self._running_process()
        assert process.stdin is not None
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _running_process(self) -> subprocess.Popen[str]:
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("chem-extract MCP is not running")
        return self.process


def load_arguments(args_json: str | None, args_file: Path | None) -> dict[str, Any]:
    if args_json is not None and args_file is not None:
        raise ValueError("use only one of --args-json and --args-file")
    if args_file is not None:
        value = json.loads(args_file.read_text(encoding="utf-8"))
    elif args_json is not None:
        value = json.loads(args_json)
    else:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be a JSON object")
    return value


def text_payload(result: dict[str, Any]) -> str:
    texts = [
        item["text"]
        for item in result.get("content", [])
        if item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    return "\n".join(texts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call the canonical chem-extract tools through their MCP boundary."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List tool names, descriptions, and schemas")

    call = subparsers.add_parser("call", help="Call one registered chemistry tool")
    call.add_argument("tool", help="Tool name, for example verify_smiles or assemble")
    arguments = call.add_mutually_exclusive_group()
    arguments.add_argument("--args-json", help="Tool arguments as one JSON object")
    arguments.add_argument("--args-file", type=Path, help="Path to a JSON object")
    call.add_argument(
        "--raw",
        action="store_true",
        help="Print the complete MCP tool-result envelope instead of its text payload",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with ChemToolClient() as client:
            if args.command == "list":
                result = client.request("tools/list", {})
                print(json.dumps(result["tools"], indent=2, ensure_ascii=False))
                return 0

            arguments = load_arguments(args.args_json, args.args_file)
            result = client.request(
                "tools/call", {"name": args.tool, "arguments": arguments}
            )
            if args.raw:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                payload = text_payload(result)
                try:
                    decoded = json.loads(payload)
                except json.JSONDecodeError:
                    print(payload)
                else:
                    print(json.dumps(decoded, indent=2, ensure_ascii=False))
            if result.get("isError"):
                return 2
            return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"chem-tool: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
