from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "figure_skills/scripts/run_pi_extract.sh"


def run(*args):
    return subprocess.run([str(SCRIPT), *map(str, args)], text=True, capture_output=True)


def test_help_documents_single_and_batch_contract():
    proc = run("--help")
    assert proc.returncode == 0
    assert "IMAGE_OR_DIR" in proc.stdout
    assert "--offline" in proc.stdout
    assert "--concurrency 1|2" in proc.stdout
    assert "--no-reviewer" in proc.stdout
    assert "default: gpt-5.6-sol" in proc.stdout
    assert "default: medium" in proc.stdout


def test_dry_run_stages_one_image_and_immutable_cache(tmp_path):
    image = tmp_path / "scope image.png"
    cache = tmp_path / "scope image.molparser.json"
    image.write_bytes(b"fake-image")
    cache.write_text('[{"bbox":[0,0,1,1],"esmi":"C"}]')
    out = tmp_path / "run"
    proc = run(image, "--offline", "--out", out, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    cases = list((out / "cases").iterdir())
    assert len(cases) == 1
    assert (cases[0] / "input/image.png").read_bytes() == b"fake-image"
    assert (cases[0] / "input/molparser.json").read_text().startswith("[")
    assert "DRY_RUN" in proc.stdout


def test_runner_registers_sealed_outputs_with_pretty_reviewer():
    source = SCRIPT.read_text()
    assert 'review "$case_root/output/pred.v2.json"' in source
    assert "--image" in source
    assert "--perception" in source
    assert "--collection figure-direct" in source


def test_offline_cache_miss_fails_visibly(tmp_path):
    image = tmp_path / "figure.png"
    image.write_bytes(b"fake-image")
    proc = run(image, "--offline", "--out", tmp_path / "run", "--dry-run")
    assert proc.returncode != 0
    assert "OFFLINE_CACHE_MISS" in proc.stderr


def test_refuses_output_overwrite(tmp_path):
    image = tmp_path / "figure.png"
    image.write_bytes(b"fake-image")
    out = tmp_path / "existing"
    out.mkdir()
    proc = run(image, "--out", out, "--dry-run")
    assert proc.returncode != 0
    assert "refusing to overwrite" in proc.stderr
