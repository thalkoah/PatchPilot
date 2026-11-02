"""Repository behavior against PostgreSQL, with no HTTP stack in the path."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tests.database import fetch_status, open_session_factory, stamp_task

from patchpilot.db.models import Task, TaskStatus
from patchpilot.repositories.tasks import TaskRepository


@pytest.fixture
async def repository(postgres_database_url: str, clean_tasks: None):
    engine, factory = open_session_factory(postgres_database_url)
    async with factory() as session:
        yield TaskRepository(session)
    await engine.dispose()


@pytest.fixture
async def load_task(postgres_database_url: str):
    engine, factory = open_session_factory(postgres_database_url)

    async def _load(task_id: UUID) -> Task | None:
        async with factory() as session:
            return await TaskRepository(session).get(task_id)

    yield _load
    await engine.dispose()


@pytest.fixture
async def list_tasks(postgres_database_url: str):
    engine, factory = open_session_factory(postgres_database_url)

    async def _list(
        *,
        status: TaskStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Task], int]:
        async with factory() as session:
            return await TaskRepository(session).list_tasks(
                status=status,
                limit=limit,
                offset=offset,
            )

    yield _list
    await engine.dispose()


async def test_create_commits_a_queued_task_visible_to_a_new_connection(
    repository: TaskRepository,
    load_task,
    postgres_database_url: str,
):
    created = await repository.create(
        title="Broken discount",
        description="Unknown tier returns 500.",
    )

    assert created.status == TaskStatus.QUEUED
    assert created.stage is None
    assert created.repo_id is None
    assert created.result_summary is None
    assert created.result_json is None
    assert created.created_at.tzinfo is not None
    assert created.updated_at.tzinfo is not None
    assert fetch_status(postgres_database_url, created.id) == "queued"

    stored = await load_task(created.id)
    assert stored is not None
    assert stored.id == created.id
    assert stored.title == "Broken discount"
    assert stored.description == "Unknown tier returns 500."
    assert stored.status == TaskStatus.QUEUED


async def test_get_unknown_id_returns_none(repository: TaskRepository):
    assert await repository.get(uuid4()) is None


async def test_list_is_newest_first_and_paginates(
    repository: TaskRepository,
    list_tasks,
    postgres_database_url: str,
):
    oldest = await repository.create(title="oldest", description="first report")
    middle = await repository.create(title="middle", description="second report")
    newest = await repository.create(title="newest", description="third report")
    stamp_task(
        postgres_database_url,
        oldest.id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    stamp_task(
        postgres_database_url,
        middle.id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        status="failed",
    )
    stamp_task(
        postgres_database_url,
        newest.id,
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    page, total = await list_tasks(status=None, limit=2, offset=0)
    assert total == 3
    assert [task.id for task in page] == [newest.id, middle.id]

    rest, rest_total = await list_tasks(status=None, limit=2, offset=2)
    assert rest_total == 3
    assert [task.id for task in rest] == [oldest.id]

    empty, empty_total = await list_tasks(status=None, limit=20, offset=50)
    assert empty_total == 3
    assert empty == []

    queued, queued_total = await list_tasks(status=TaskStatus.QUEUED, limit=20, offset=0)
    assert queued_total == 2
    assert [task.id for task in queued] == [newest.id, oldest.id]

    failed, failed_total = await list_tasks(status=TaskStatus.FAILED, limit=20, offset=0)
    assert failed_total == 1
    assert failed[0].id == middle.id
    assert failed[0].status == TaskStatus.FAILED

    rejected, rejected_total = await list_tasks(status=TaskStatus.REJECTED, limit=20, offset=0)
    assert rejected_total == 0
    assert rejected == []
