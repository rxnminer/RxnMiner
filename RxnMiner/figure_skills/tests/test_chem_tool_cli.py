import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "chem_tool.py"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_cli_lists_the_canonical_eight_tools():
    result = run_cli("list")
    assert result.returncode == 0, result.stderr
    tools = json.loads(result.stdout)
    assert {tool["name"] for tool in tools} == {
        "detect",
        "ocsr_detect",
        "reverse_rgroup",
        "fill_rgroup",
        "reverse_assemble",
        "verify_smiles",
        "assemble",
        "render_reactions",
    }


def test_cli_calls_verify_smiles_through_mcp():
    result = run_cli("call", "verify_smiles", "--args-json", '{"smiles":"CCO"}')
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["canonical_iso"] == "CCO"


def test_cli_assembles_a_large_plan_by_path(tmp_path):
    plan_path = tmp_path / "plan.json"
    output_path = tmp_path / "out.v2.json"
    arguments_path = tmp_path / "args.json"
    plan_path.write_text(
        json.dumps(
            {
                "selected_examples": [
                    {
                        "identifier": "row-1",
                        "reactants": [{"identifier": "A", "smiles": "CCO"}],
                        "products": [{"identifier": "B", "smiles": "CC=O"}],
                        "conditions": [[{"role": "reagent", "text": "test"}]],
                    }
                ],
                "reaction_templates": [],
            }
        )
    )
    arguments_path.write_text(
        json.dumps({"plan_path": str(plan_path), "out_path": str(output_path)})
    )

    result = run_cli("call", "assemble", "--args-file", str(arguments_path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["n_examples"] == 1
    assert output_path.is_file()


def test_cli_rejects_non_object_arguments_before_calling_tool():
    result = run_cli("call", "verify_smiles", "--args-json", "[]")
    assert result.returncode == 1
    assert "tool arguments must be a JSON object" in result.stderr
