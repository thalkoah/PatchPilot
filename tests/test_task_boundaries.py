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


def test_request_path_does_not_call_a_model():
    for relative in (
        "src/patchpilot/api.py",
        "src/patchpilot/routes/tasks.py",
        "src/patchpilot/worker.py",
        "src/patchpilot/dependencies.py",
    ):
        modules = imported_modules(relative)
        assert not any(
            name == "patchpilot.llm" or name.startswith("patchpilot.llm.") for name in modules
        ), relative


def test_initial_migration_freezes_column_limits_and_rejected_status():
    text = (ROOT / "alembic/versions/20261102_0001_create_tasks.py").read_text(encoding="utf-8")

    assert TITLE_MAX_LENGTH == 200
    assert DESCRIPTION_MAX_LENGTH == 20_000
    assert "sa.String(length=200)" in text
    assert "char_length(description) <= 20000" in text
    assert '"rejected"' in text
