from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "figure_skills" / "skills" / "reaction-extract" / "SKILL.md"
LESSONS = ROOT / "figure_skills" / "skills" / "reaction-extract" / "LESSONS.md"
RUNTIME_SKILLS = ROOT / "runtime" / "pi-service" / "src" / "skills.ts"


def test_skill_makes_current_pi_the_only_model_decision_maker():
    source = SKILL.read_text()
    assert "You, the current Pi, are the extractor" in source
    assert "one-shot VLM" in source
    assert "workflow/subagent" in source
    assert "hidden helper model" in source
    assert "This does not forbid visual reasoning" in source


def test_skill_separates_sealed_agent_score_from_reviewed_uplift():
    source = SKILL.read_text()
    assert "seal the raw prediction before GT access" in source
    assert "A reviewed uplift is not agent accuracy" in source
    lessons = LESSONS.read_text()
    assert "reviewed uplift measures human correction, not agent accuracy" in lessons


def test_lessons_capture_multistep_reverse_chain_and_coordination_guards():
    source = LESSONS.read_text()
    assert "explicit intermediate followed by a new condition arrow" in source
    assert "Exact product roundtrip is necessary but not sufficient" in source
    assert "insertion/deletion" in source
    assert "A drawn coordination closure is chemical connectivity" in source
    assert 'ocsr_audit.decision:"accepted"' in source
    assert "Tagged E-SMILES preservation is not concrete validation" in source
    assert "require_concrete:true" in source
    assert "multiple templates do not excuse an empty graph" in source
    assert "fail closed" in source


def test_runtime_pins_lessons_as_skill_support():
    source = RUNTIME_SKILLS.read_text()
    assert '"figure_skills/skills/reaction-extract/LESSONS.md"' in source
    assert "ff29c2f0b5e23d74e9b8e83be5b0420745441dcb3f28c3cdb1af6db56d485447" in source
