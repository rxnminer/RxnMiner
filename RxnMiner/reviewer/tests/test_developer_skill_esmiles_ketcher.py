from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / ".agents" / "skills" / "chemrxn-esmiles-ketcher"
SKILL = SKILL_DIR / "SKILL.md"
REFERENCE = SKILL_DIR / "references" / "ketcher-roundtrip.md"
SMOKE = SKILL_DIR / "scripts" / "smoke-roundtrip.mjs"


def test_developer_skill_is_complete_and_routes_to_canonical_sources():
    source = SKILL.read_text()
    assert source.startswith("---\nname: chemrxn-esmiles-ketcher\n")
    assert "description:" in source.split("---", 2)[1]
    for relative in (
        "../../../figure_skills/skills/reaction-extract/references/extended-smiles-2.0.md",
        "../../../reviewer/vendor/KETCHER_RELEASE.md",
        "references/ketcher-roundtrip.md",
    ):
        assert relative in source
        assert (SKILL_DIR / relative).resolve().is_file()


def test_developer_skill_captures_lossless_editor_boundary():
    source = REFERENCE.read_text()
    for required in (
        "setMolecule(fullEsmiles)",
        "getEsmiles()",
        "_source.ocsr_esmi",
        "reviewed sidecar",
        "Byte equality versus semantic roundtrip",
        "Body-only RDKit preview",
        "Markush Ring",
        "Virtual Arc",
        "5edb8fae589e28068e8560873de12cf13b3bd5d986ebad0b67dd59d170b54ed0",
    ):
        assert required in source


def test_browser_smoke_covers_extension_categories():
    source = SMOKE.read_text()
    for required in (
        "plain",
        "empty-extension",
        "atom-group",
        "dummy",
        "ring-position",
        "abstract-circle",
        "virtual-arc",
        "repeat-sgroup",
        "nested-s",
        "getEsmiles",
    ):
        assert required in source


def test_developer_skill_is_not_in_product_pi_allowlist():
    settings = (ROOT / ".pi" / "settings.json").read_text()
    package = (ROOT / "package.json").read_text()
    assert ".agents/skills" not in settings
    assert "chemrxn-esmiles-ketcher" not in package


def test_large_owner_sources_are_not_bundled_into_skill():
    forbidden = {".pdf", ".tar", ".gz", ".wasm"}
    assert not [path for path in SKILL_DIR.rglob("*") if path.suffix.lower() in forbidden]
