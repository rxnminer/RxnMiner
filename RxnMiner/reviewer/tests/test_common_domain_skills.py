from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / ".agents" / "skills"
CLAUDE = ROOT / ".claude" / "skills"
CODEX = ROOT / ".codex" / "skills"
NAMES = (
    "figure-extraction",
    "esmiles-2.0",
    "fhr-rgroup",
    "perception-tools",
    "ui-visualization",
    "annotation-review",
    "annotation-platform-benchmark",
    "benchmark-review-ops",
    "extraction-evaluation",
    "smiles-source-review",
    "review-report",
)


def test_common_domain_skills_are_complete_and_discoverable():
    for name in NAMES:
        source = (COMMON / name / "SKILL.md").read_text()
        assert source.startswith(f"---\nname: {name}\n")
        assert "description:" in source.split("---", 2)[1]
        assert "STATUS: stub" not in source
        assert "To fill" not in source


def test_claude_and_codex_wrappers_delegate_to_common_skills():
    for name in (*NAMES, "ketcher-drawboard"):
        claude = (CLAUDE / "domain" / name / "SKILL.md").read_text()
        codex = (CODEX / "domain" / name / "SKILL.md").read_text()
        assert claude == codex
        assert "STATUS: stub" not in claude
        assert "To fill" not in claude
        if name == "ketcher-drawboard":
            assert ".agents/skills/chemrxn-esmiles-ketcher/SKILL.md" in claude
        else:
            assert f".agents/skills/{name}/SKILL.md" in claude


def test_review_and_evaluation_skills_preserve_method_boundaries():
    review = (COMMON / "annotation-review" / "SKILL.md").read_text()
    evaluation = (COMMON / "extraction-evaluation" / "SKILL.md").read_text()
    adjudication = (
        COMMON / "annotation-review" / "references" / "adjudication-contract.md"
    ).read_text()
    ingest = (
        COMMON / "annotation-review" / "references" / "acceptance-workflow-ingest.md"
    ).read_text()
    attribution = (
        COMMON / "extraction-evaluation" / "references" / "error-attribution.md"
    ).read_text()
    protocol = (
        COMMON / "extraction-evaluation" / "references" / "sealed-run-protocol.md"
    ).read_text()

    assert "accepted revision is a candidate" in review
    assert "Ketcher display or roundtrip" in review
    assert "source-adjudicated reference" in review
    assert "Model-agnostic attribution" in evaluation
    assert "Seal all artifacts" in evaluation
    assert "perception correct → system broke" in evaluation
    assert "Do not blame FHR" in attribution
    assert "Never overwrite a seal" in protocol
    assert "If only disagreements were reviewed" in adjudication
    assert "mode=ro" in ingest
    assert "accepted_payload_sha256" in ingest


def test_smiles_source_review_skill_is_reusable_and_mask_safe():
    skill = (COMMON / "smiles-source-review" / "SKILL.md").read_text()
    protocol = (
        COMMON / "smiles-source-review" / "references" / "independent-session-protocol.md"
    ).read_text()
    renderer = (
        COMMON / "smiles-source-review" / "scripts" / "render_smiles_review.py"
    ).read_text()
    assert "one fresh isolated reviewer session per page" in skill
    assert "Human mask regions are outside annotation scope" in skill
    assert "source figure or PDF is authoritative" in skill
    assert "one `review.html` per page" in skill
    assert "--no-context-files --no-skills" in protocol
    assert "Masked content is not correct, incorrect, missing, extra, or unresolved" in protocol
    assert "--workspace" in renderer
    assert "source_evidence" in renderer
    assert "render_structure" in renderer


def test_review_report_skill_is_postseal_and_fail_closed():
    skill = (COMMON / "review-report" / "SKILL.md").read_text()
    contract = (
        COMMON / "review-report" / "references" / "report-contract.md"
    ).read_text()
    renderer = (
        COMMON / "review-report" / "scripts" / "render_review_report.py"
    ).read_text()
    assert "presentation layer, never an adjudicator" in skill
    assert "Blind mode accepts exactly one reviewer and one pass" in skill
    assert "unlocked: true" in skill
    assert "Blind HTML must not infer or display another candidate lane" in contract
    assert "assert_review_seal" in renderer
    assert "Coordinator report manifest is not explicitly unlocked" in renderer


def test_benchmark_review_ops_is_coordinator_only_and_complete():
    skill = (COMMON / "benchmark-review-ops" / "SKILL.md").read_text()
    issue_contract = (
        COMMON / "benchmark-review-ops" / "references" / "github-issue-contract.md"
    ).read_text()
    feishu_contract = (
        COMMON / "benchmark-review-ops" / "references" / "feishu-status-contract.md"
    ).read_text()
    assert "must never enter Product extraction allowlists" in skill
    assert "Full primary and overlap ID lists" in skill
    assert "Generation fails" in issue_contract
    assert "Plan before write" in feishu_contract


def test_repository_development_skill_is_shared_but_not_product_loaded():
    source = (COMMON / "repo-development" / "SKILL.md").read_text()
    claude = (CLAUDE / "process" / "repo-development" / "SKILL.md").read_text()
    codex = (CODEX / "process" / "repo-development" / "SKILL.md").read_text()
    assert source.startswith("---\nname: repo-development\n")
    assert ".agents/skills/repo-development/SKILL.md" in claude
    assert claude == codex
    assert "must not enter Product extraction allowlists" in source


def test_common_developer_skills_stay_out_of_product_pi_allowlist():
    settings = (ROOT / ".pi" / "settings.json").read_text()
    package = (ROOT / "package.json").read_text()
    assert ".agents/skills" not in settings
    assert "repo-development" not in package
    for name in NAMES:
        assert name not in package
