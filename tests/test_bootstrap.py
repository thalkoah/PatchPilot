import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap.sh"


def test_bootstrap_script_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_bootstrap_help_exits_before_installing_anything():
    completed = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "LLM_STAGE_MODELS" in completed.stdout
    assert completed.stderr == ""


def test_bootstrap_rejects_an_unknown_argument():
    completed = subprocess.run(
        ["bash", str(SCRIPT), "--nope"],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "unknown argument" in completed.stderr


def test_bootstrap_discovers_models_instead_of_naming_them():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "python -m patchpilot.llm.required_models" in text
    assert "qwen" not in text.casefold()
    assert "gemma" not in text.casefold()
    assert "fedora" in text.casefold()
    assert "Darwin" in text
    assert "NoNewPrivs" in text
    assert 'up -d' in text
    assert "API is already ready" in text
