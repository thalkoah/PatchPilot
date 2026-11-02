"""PostgreSQL persistence for bug-report tasks."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.db.models import Task, TaskStatus


class TaskRepository:
    """Reads and writes `tasks`. `create` commits so the row survives the session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, title: str, description: str) -> Task:
        task = Task(title=title, description=description, status=TaskStatus.QUEUED)
        self._session.add(task)
        await self._session.commit()
        await self._session.refresh(task)
        return task

    async def get(self, task_id: UUID) -> Task | None:
        return await self._session.get(Task, task_id)

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Task], int]:
        count_stmt = select(func.count()).select_from(Task)
        list_stmt = select(Task).order_by(Task.created_at.desc(), Task.id.desc())
        if status is not None:
            count_stmt = count_stmt.where(Task.status == status)
            list_stmt = list_stmt.where(Task.status == status)
        total = await self._session.scalar(count_stmt)
        rows = await self._session.scalars(list_stmt.limit(limit).offset(offset))
        return list(rows), int(total or 0)
