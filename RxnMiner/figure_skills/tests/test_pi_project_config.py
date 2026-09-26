import json
from pathlib import Path


def test_pi_settings_discovers_single_skill_and_extension_paths():
    root = Path(__file__).resolve().parents[2]
    settings = json.loads((root / ".pi" / "settings.json").read_text())
    assert settings["skills"] == ["../figure_skills/skills"]
    assert settings["extensions"] == ["../figure_skills/pi-extension/index.ts"]
    assert (root / "figure_skills" / "skills" / "reaction-extract" / "SKILL.md").is_file()
    assert (root / "figure_skills" / "pi-extension" / "index.ts").is_file()


def test_pi_bridge_registers_all_shared_mcp_tools():
    root = Path(__file__).resolve().parents[2]
    extension = (root / "figure_skills" / "pi-extension" / "index.ts").read_text()
    expected = {
        "detect",
        "ocsr_detect",
        "reverse_rgroup",
        "fill_rgroup",
        "reverse_assemble",
        "verify_smiles",
        "assemble",
        "render_reactions",
    }
    for name in expected:
        assert f"{name}: Type.Object" in extension
    assert 'resolve(EXTENSION_DIR, "..")' in extension
    assert 'scripts", "run_chem_mcp.py"' in extension
