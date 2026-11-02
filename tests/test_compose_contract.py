from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compose_starts_database_api_and_idle_worker():
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "image: docker.io/library/postgres:16-alpine" in text
    assert "127.0.0.1:8000:8000" in text
    assert "condition: service_healthy" in text
    assert 'command: ["python", "-m", "patchpilot.worker"]' in text
    for service in ("db:", "api:", "worker:"):
        assert service in text


def test_project_dependencies_exclude_model_frameworks():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = text.split("[dependency-groups]", 1)[0]
    for name in ("langgraph", "openai", "ollama", "langchain"):
        assert name not in dependencies


def test_env_example_is_present_and_env_is_ignored():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "DATABASE_URL=" in example
    assert "patchpilot_dev" in example
    assert ".env" in ignore.splitlines()
