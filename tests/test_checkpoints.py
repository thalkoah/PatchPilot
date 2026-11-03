"""Postgres checkpoints are separate from task rows and survive a new process."""

import json
import logging
import os
import subprocess
import sys
import textwrap
from uuid import UUID, uuid4

import psycopg
import pytest
from tests.database import open_session_factory
from tests.workflow_support import compiled_worker_graph

from patchpilot.checkpoints import checkpointer_conninfo
from patchpilot.config import Settings, get_settings
from patchpilot.db.models import TaskStatus
from patchpilot.llm.fake import DISCOUNT_TIER_TRIAGE, FakeProvider
from patchpilot.llm.triage import TriageResult
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.worker import serve
from patchpilot.workflow.graph import (
    initial_triage_state,
    invoke_triage,
    read_thread_state,
    thread_config,
)

CHECKPOINT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)
TASK_COLUMNS = {
    "id",
    "title",
    "description",
    "repo_id",
    "status",
    "stage",
    "result_summary",
    "result_json",
    "created_at",
    "updated_at",
}
DEMO_TITLE = "Unknown customer tier crashes discount endpoint"
DEMO_DESCRIPTION = "An unrecognized customer tier returns HTTP 500."

WRITER = textwrap.dedent(
    """
    import asyncio
    import os
    from uuid import UUID

    from patchpilot.checkpoints import open_checkpointer, setup_checkpoints
    from patchpilot.config import Settings
    from patchpilot.db.session import create_engine, create_session_factory
    from patchpilot.llm.fake import FakeProvider
    from patchpilot.worker import process_task
    from patchpilot.workflow.graph import compile_triage_graph

    async def main() -> None:
        database_url = os.environ["DATABASE_URL"]
        task_id = UUID(os.environ["TASK_ID"])
        settings = Settings(database_url=database_url, llm_provider="fake", _env_file=None)
        await setup_checkpoints(database_url)
        engine = create_engine(settings)
        try:
            factory = create_session_factory(engine)
            async with open_checkpointer(database_url) as checkpointer:
                claimed = await process_task(
                    factory,
                    FakeProvider(),
                    task_id,
                    compile_triage_graph(checkpointer),
                )
            if not claimed:
                raise SystemExit(2)
        finally:
            await engine.dispose()

    asyncio.run(main())
    """
)

READER = textwrap.dedent(
    """
    import asyncio
    import json
    import os
    from uuid import UUID

    from patchpilot.checkpoints import open_checkpointer
    from patchpilot.workflow.graph import compile_triage_graph, read_thread_state

    async def main() -> None:
        task_id = UUID(os.environ["TASK_ID"])
        async with open_checkpointer(os.environ["DATABASE_URL"]) as checkpointer:
            values = await read_thread_state(compile_triage_graph(checkpointer), task_id)
        print(json.dumps({
            "task_id": values.get("task_id"),
            "result_summary": values.get("result_summary"),
        }))

    asyncio.run(main())
    """
)


class NumberedProvider:
    """Each call would change the summary. A resume must not make that call."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, prompt: str, schema: type, options: object = None):
        self.calls += 1
        return DISCOUNT_TIER_TRIAGE.model_copy(update={"summary": f"pass-{self.calls}"})

    def close(self) -> None:
        return None


def test_thread_id_is_the_task_uuid():
    task_id = UUID("6f0d5c6e-3b1a-4f7e-9c2d-1a2b3c4d5e6f")

    assert thread_config(task_id) == {
        "configurable": {"thread_id": "6f0d5c6e-3b1a-4f7e-9c2d-1a2b3c4d5e6f"}
    }


def test_checkpointer_connection_is_named_without_changing_the_database():
    url = "postgresql://patchpilot:patchpilot_dev@db:5432/patchpilot"

    named = checkpointer_conninfo(url)

    assert named.startswith(url)
    assert named.endswith("?application_name=patchpilot_checkpointer")
    assert checkpointer_conninfo(named) == named


def test_checkpoint_command_rejects_a_non_postgres_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/patchpilot.db")
    get_settings.cache_clear()
    from patchpilot.checkpoints import main

    assert main() == 2


def test_checkpoint_command_is_idempotent(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    get_settings.cache_clear()
    from patchpilot.checkpoints import main

    with caplog.at_level(logging.INFO):
        assert main() == 0
        assert main() == 0

    assert "checkpoint tables ready" in caplog.text
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        migrations = connection.execute("SELECT v FROM checkpoint_migrations ORDER BY v").fetchall()
    assert version is not None
    assert version[0] == "0001_create_tasks"
    assert migrations
    assert all(isinstance(row[0], int) for row in migrations)


async def test_resume_does_not_repeat_the_user_facing_summary(postgres_database_url: str):
    task_id = uuid4()
    state = initial_triage_state(
        task_id=task_id,
        title=DEMO_TITLE,
        description=DEMO_DESCRIPTION,
    )
    first = NumberedProvider()

    async with compiled_worker_graph(postgres_database_url, interrupt_after=["triage"]) as graph:
        paused = await invoke_triage(graph, state, task_id=task_id, provider=first)
        snapshot = await graph.aget_state(thread_config(task_id))

    assert first.calls == 1
    assert not paused.get("result_summary")
    assert snapshot.next == ("finalize",)
    paused_ids = _checkpoint_ids(postgres_database_url, task_id)
    assert paused_ids

    second = NumberedProvider()
    async with compiled_worker_graph(postgres_database_url, interrupt_after=["triage"]) as graph:
        resumed = await invoke_triage(
            graph,
            None,
            task_id=task_id,
            provider=second,
            resume=True,
        )
        finished = await graph.aget_state(thread_config(task_id))

    assert second.calls == 0
    assert resumed["result_summary"] == "pass-1"
    assert resumed["result_summary"].count("pass-1") == 1
    assert finished.next == ()
    resumed_ids = _checkpoint_ids(postgres_database_url, task_id)
    assert len(resumed_ids) > len(paused_ids)

    third = NumberedProvider()
    async with compiled_worker_graph(postgres_database_url, interrupt_after=["triage"]) as graph:
        again = await invoke_triage(
            graph,
            None,
            task_id=task_id,
            provider=third,
            resume=True,
        )

    assert third.calls == 0
    assert again["result_summary"] == "pass-1"
    assert _checkpoint_ids(postgres_database_url, task_id) == resumed_ids


async def test_completed_checkpoint_loads_in_a_new_process(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            created = await TaskRepository(session).create(
                title=DEMO_TITLE,
                description=DEMO_DESCRIPTION,
            )
        task_id = created.id
    finally:
        await engine.dispose()

    env = os.environ.copy()
    env["DATABASE_URL"] = postgres_database_url
    env["TASK_ID"] = str(task_id)
    env["LLM_PROVIDER"] = "fake"
    _run_process(WRITER, env)
    loaded = json.loads(_run_process(READER, env))

    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            stored = await TaskRepository(session).get(task_id)
    finally:
        await engine.dispose()

    assert stored is not None
    assert stored.status == TaskStatus.COMPLETED
    assert stored.result_summary == DISCOUNT_TIER_TRIAGE.summary
    assert set(stored.result_json or {}) == set(TriageResult.model_fields)
    assert loaded == {
        "task_id": str(task_id),
        "result_summary": stored.result_summary,
    }
    _assert_checkpoint_is_not_the_task_row(postgres_database_url, task_id, stored.result_json)


async def test_worker_closes_the_checkpointer_when_it_stops(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            created = await TaskRepository(session).create(
                title=DEMO_TITLE,
                description=DEMO_DESCRIPTION,
            )
    finally:
        await engine.dispose()

    async def stop(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("patchpilot.worker.asyncio.sleep", stop)
    settings = Settings(
        database_url=postgres_database_url,
        llm_provider="fake",
        worker_idle_sleep_seconds=2,
        _env_file=None,
    )
    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        await serve(settings, provider=FakeProvider())

    assert "checkpointer=postgres" in caplog.text
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        open_savers = connection.execute(
            """
            SELECT count(*)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND application_name = 'patchpilot_checkpointer'
            """
        ).fetchone()
    assert open_savers is not None
    assert open_savers[0] == 0

    async with compiled_worker_graph(postgres_database_url) as graph:
        values = await read_thread_state(graph, created.id)
    assert values["task_id"] == str(created.id)
    assert values["result_summary"] == DISCOUNT_TIER_TRIAGE.summary


def _run_process(script: str, env: dict[str, str]) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _checkpoint_ids(database_url: str, task_id: UUID) -> list[str]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        rows = connection.execute(
            """
            SELECT checkpoint_id
            FROM checkpoints
            WHERE thread_id = %s
            ORDER BY checkpoint_id
            """,
            (str(task_id),),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _assert_checkpoint_is_not_the_task_row(
    database_url: str,
    task_id: UUID,
    result_json: object,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        tables = connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        ).fetchall()
        columns = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'tasks'
            """
        ).fetchall()
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        checkpoint = connection.execute(
            """
            SELECT thread_id, checkpoint
            FROM checkpoints
            WHERE thread_id = %s
            ORDER BY checkpoint_id DESC
            LIMIT 1
            """,
            (str(task_id),),
        ).fetchone()
    names = {row[0] for row in tables}
    assert set(CHECKPOINT_TABLES) <= names
    assert "tasks" in names
    assert {row[0] for row in columns} == TASK_COLUMNS
    assert version is not None
    assert version[0] == "0001_create_tasks"
    assert checkpoint is not None
    assert checkpoint[0] == str(task_id)
    assert checkpoint[1] != result_json
    assert "channel_versions" in checkpoint[1]
    assert "channel_versions" not in json.dumps(result_json)
