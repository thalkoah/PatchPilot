from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


API_COMMAND = (
    "alembic upgrade head && exec uvicorn patchpilot.api:create_app "
    "--factory --host 0.0.0.0 --port 8000"
)


def test_compose_starts_database_api_and_worker():
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "image: docker.io/library/postgres:16-alpine" in text
    assert "127.0.0.1:8000:8000" in text
    assert "condition: service_healthy" in text
    assert 'command: ["python", "-m", "patchpilot.worker"]' in text
    assert API_COMMAND in text
    for service in ("db:", "api:", "worker:"):
        assert service in text


def test_api_image_runs_migrations_before_serving():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY --chown=app:app alembic.ini /app/alembic.ini" in dockerfile
    assert "COPY --chown=app:app alembic /app/alembic" in dockerfile
    assert API_COMMAND in dockerfile


def test_direct_dependencies_include_langgraph_and_exclude_cloud_sdks():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = text.split("[dependency-groups]", 1)[0]

    assert '"langgraph' in dependencies
    assert '"langgraph-checkpoint-postgres' in dependencies
    for name in ("openai", "ollama", "langchain", "langchain-openai", "langchain-core"):
        assert f'"{name}' not in dependencies


def test_worker_reaches_host_ollama_only_through_the_documented_gateway():
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    api, worker = text.split("\n  worker:\n", 1)

    assert "OLLAMA_BASE_URL" not in api
    assert "LLM_PROVIDER" not in api
    assert "host.docker.internal:host-gateway" in worker
    assert "LLM_PROVIDER: ${LLM_PROVIDER:-fake}" in worker
    assert "WORKER_IDLE_SLEEP_SECONDS: ${WORKER_IDLE_SLEEP_SECONDS:-2}" in worker
    assert "WORKER_LEASE_SECONDS: ${WORKER_LEASE_SECONDS:-30}" in worker
    assert "WORKER_MAX_ATTEMPTS: ${WORKER_MAX_ATTEMPTS:-3}" in worker
    assert "INVESTIGATOR_MAX_TOOL_CALLS: ${INVESTIGATOR_MAX_TOOL_CALLS:-6}" in worker
    assert "OLLAMA_BASE_URL: ${OLLAMA_BASE_URL:-http://host.docker.internal:11434}" in worker
    assert "11434" not in api


def test_readme_warns_about_ollama_network_bindings():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").casefold()

    assert "host.docker.internal" in readme
    assert "host-gateway" in readme
    assert "11434" in readme
    assert "firewalld" in readme
    assert "lan" in readme


def test_readme_documents_checkpoint_setup_outside_alembic():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "python -m patchpilot.checkpoints" in readme
    assert "python -m patchpilot.checkpoints" in example
    assert "stay out of Alembic" in readme
    assert "thread_id" in readme


def test_worker_mounts_registered_sources_read_only():
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    api, worker = text.split("\n  worker:\n", 1)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "REPOSITORY_ROOT: /repositories" in worker
    assert "${REPOSITORY_ROOT_HOST:-./examples}:/repositories:ro" in worker
    assert "/repositories" not in api
    assert "python -m patchpilot.source" in readme
    assert ":ro" in readme
    assert "REPOSITORY_ROOT_HOST=./examples" in example


def test_plan_artifacts_are_shared_by_the_api_and_the_worker():
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    api, worker = text.split("\n  worker:\n", 1)

    assert "ARTIFACTS_ROOT: /artifacts" in api
    assert "ARTIFACTS_ROOT: /artifacts" in worker
    assert "artifacts:/artifacts" in api
    assert "artifacts:/artifacts" in worker
    assert "GET /api/v1/tasks/{id}/plan" in readme
    assert "ARTIFACTS_ROOT" in example


def test_env_example_is_present_and_env_is_ignored():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "DATABASE_URL=" in example
    assert "LLM_PROVIDER=fake" in example
    assert "WORKER_IDLE_SLEEP_SECONDS=2" in example
    assert "WORKER_LEASE_SECONDS=30" in example
    assert "WORKER_MAX_ATTEMPTS=3" in example
    assert "INVESTIGATOR_MAX_TOOL_CALLS=6" in example
    assert "patchpilot_dev" in example
    assert ".env" in ignore.splitlines()
