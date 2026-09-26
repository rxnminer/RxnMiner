from pathlib import Path

from tools.chem_mcp import tool_verify_smiles


def test_verify_smiles_writes_viewable_png_renders():
    result = tool_verify_smiles("CCOC(=O)CCCCCCCCCc1ccc(OC)cc1")

    assert result["valid"] is True
    assert result["formula"] == "C19H30O3"
    for key in ("render_png_path", "indexed_render_png_path"):
        path = Path(result[key])
        assert path.is_file()
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_verification_skill_requires_source_render_comparison():
    skill = Path(__file__).parents[1] / "skills" / "reaction-verify" / "SKILL.md"
    text = skill.read_text()

    assert "Open both `render_png_path` and `indexed_render_png_path`" in text
    assert "never overwrites the candidate" in text
    assert "FHR closure proves only deterministic consistency" in text
    assert "another model/system's candidate string" in text


def test_pi_extension_attaches_both_verification_pngs():
    extension = (Path(__file__).parents[1] / "pi-extension" / "index.ts").read_text()

    assert 'for (const key of ["render_png_path", "indexed_render_png_path"]' in extension
    assert 'type: "image"' in extension
    assert 'mimeType: "image/png"' in extension
    assert "await readFile(imagePath)" in extension


def test_extraction_skill_has_generic_inventory_and_verification_handoff():
    skill = Path(__file__).parents[1] / "skills" / "reaction-extract" / "SKILL.md"
    text = skill.read_text()

    assert "independent source inventory" in text
    assert "emitted, explicitly excluded, masked, or unresolved" in text
    assert "automatically attached clean and atom-indexed PNG" in text
    assert "immutable candidate before any optional verification stage" in text
    assert "Mechanistic" in text and "plausibility, product closure, or FHR roundtrip alone cannot upgrade" in text
