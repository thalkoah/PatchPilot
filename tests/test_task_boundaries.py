"""Layer boundaries: handlers, DTOs, and the repository do not absorb each other."""

import ast
from pathlib import Path

from patchpilot.db.models import DESCRIPTION_MAX_LENGTH, TITLE_MAX_LENGTH

ROOT = Path(__file__).resolve().parents[1]


def imported_modules(relative_path: str) -> set[str]:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_task_handlers_delegate_to_the_repository():
    modules = imported_modules("src/patchpilot/routes/tasks.py")

    assert "patchpilot.repositories.tasks" in modules
    assert "patchpilot.schemas.tasks" in modules
    assert not any(name == "sqlalchemy" or name.startswith("sqlalchemy.") for name in modules)
    assert "psycopg" not in modules


def test_repository_does_not_depend_on_http_or_dtos():
    modules = imported_modules("src/patchpilot/repositories/tasks.py")

    assert "patchpilot.db.models" in modules
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in modules)
    assert not any(name.startswith("patchpilot.schemas") for name in modules)
    assert not any(name.startswith("patchpilot.routes") for name in modules)


def test_dtos_are_not_the_http_or_repository_layer():
    modules = imported_modules("src/patchpilot/schemas/tasks.py")

    assert "patchpilot.db.models" in modules
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in modules)
    assert not any(name.startswith("patchpilot.repositories") for name in modules)
    assert not any(name.startswith("patchpilot.routes") for name in modules)


def test_request_path_does_not_call_a_model_or_schedule_background_work():
    for relative in (
        "src/patchpilot/api.py",
        "src/patchpilot/routes/tasks.py",
        "src/patchpilot/dependencies.py",
    ):
        modules = imported_modules(relative)
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "BackgroundTasks" not in source
        assert not any(
            name == "patchpilot.llm" or name.startswith("patchpilot.llm.") for name in modules
        ), relative
        assert not any(name == "langgraph" or name.startswith("langgraph.") for name in modules)
        assert not any(name.startswith("patchpilot.workflow") for name in modules), relative
        assert "patchpilot.checkpoints" not in modules


def test_worker_runs_the_graph_outside_the_api():
    modules = imported_modules("src/patchpilot/worker.py")
    source = (ROOT / "src/patchpilot/worker.py").read_text(encoding="utf-8")

    assert "patchpilot.workflow.graph" in modules
    assert "patchpilot.checkpoints" in modules
    assert "patchpilot.llm.routing" in modules
    assert "BackgroundTasks" not in source
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in modules)


def test_triage_graph_does_not_touch_the_database_or_http():
    modules = imported_modules("src/patchpilot/workflow/graph.py")

    assert "langgraph.graph" in modules
    assert "patchpilot.checkpoints" not in modules
    assert not any("checkpoint.postgres" in name for name in modules)
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in modules)
    assert not any(name.startswith("patchpilot.repositories") for name in modules)
    assert not any(name == "sqlalchemy" or name.startswith("sqlalchemy.") for name in modules)
    assert "psycopg" not in modules


def test_job_repository_does_not_depend_on_http_or_the_graph():
    modules = imported_modules("src/patchpilot/repositories/jobs.py")

    assert "patchpilot.db.models" in modules
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in modules)
    assert not any(name.startswith("patchpilot.workflow") for name in modules)
    assert not any(name.startswith("patchpilot.schemas") for name in modules)
    assert not any(name == "langgraph" or name.startswith("langgraph.") for name in modules)


def test_initial_migration_freezes_column_limits_and_rejected_status():
    text = (ROOT / "alembic/versions/20261102_0001_create_tasks.py").read_text(encoding="utf-8")

    assert TITLE_MAX_LENGTH == 200
    assert DESCRIPTION_MAX_LENGTH == 20_000
    assert "sa.String(length=200)" in text
    assert "char_length(description) <= 20000" in text
    assert '"rejected"' in text


def test_read_only_tools_do_not_touch_the_database_or_http():
    modules = imported_modules("src/patchpilot/source/tools.py")
    source = (ROOT / "src/patchpilot/source/tools.py").read_text(encoding="utf-8")

    assert "patchpilot.source.paths" in modules
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in modules)
    assert not any(name == "sqlalchemy" or name.startswith("sqlalchemy.") for name in modules)
    assert not any(name.startswith("patchpilot.repositories") for name in modules)
    assert not any(name.startswith("patchpilot.db") for name in modules)
    assert "write_text" not in source
    assert "write_bytes" not in source


def test_checkpoint_setup_is_not_an_alembic_migration():
    modules = imported_modules("src/patchpilot/checkpoints.py")
    alembic_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "alembic").rglob("*.py")
    )

    assert "langgraph.checkpoint.postgres.aio" in modules
    assert not any(name == "alembic" or name.startswith("alembic.") for name in modules)
    assert not any(name.startswith("patchpilot.repositories") for name in modules)
    assert not any(name.startswith("patchpilot.db") for name in modules)
    assert "checkpoint_migrations" not in alembic_text
    assert "checkpoint_blobs" not in alembic_text
