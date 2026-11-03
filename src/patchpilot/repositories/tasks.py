"""PostgreSQL persistence for bug-report tasks."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.db.models import STAGE_MAX_LENGTH, Task, TaskStatus


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    """A queued row this worker owns. The claim transaction is already committed."""

    id: UUID
    title: str
    description: str


class TaskRepository:
    """Reads and writes `tasks`. Mutating methods commit so the row survives the session."""

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

    async def claim_next_queued(self, *, stage: str) -> ClaimedTask | None:
        """Claim the oldest queued task and commit before the caller returns.

        The row lock ends at that commit. Callers must not run a model call
        until this session is closed.
        """
        statement = (
            select(Task)
            .where(Task.status == TaskStatus.QUEUED)
            .order_by(Task.created_at.asc(), Task.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return await self._claim(statement, stage=stage)

    async def claim_queued(self, task_id: UUID, *, stage: str) -> ClaimedTask | None:
        """Claim one queued task by id. Return None when it is no longer queued."""
        statement = (
            select(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return await self._claim(statement, stage=stage)

    async def save_triage_result(
        self,
        task_id: UUID,
        *,
        status: TaskStatus,
        stage: str,
        result_summary: str,
        result_json: dict[str, Any],
    ) -> None:
        """Store a terminal triage outcome for a task this worker has claimed."""
        if status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            raise ValueError("triage persistence only accepts completed or failed")
        _require_stage(stage)
        if not result_summary.strip():
            raise ValueError("result_summary must not be blank")
        task = await self._session.get(Task, task_id)
        if task is None:
            raise LookupError(f"task {task_id} was not found")
        if task.status != TaskStatus.RUNNING:
            raise RuntimeError(f"task {task_id} is {task.status.value}, not running")
        task.status = status
        task.stage = stage
        task.result_summary = result_summary
        task.result_json = result_json
        await self._session.commit()

    async def _claim(self, statement: Any, *, stage: str) -> ClaimedTask | None:
        _require_stage(stage)
        task = await self._session.scalar(statement)
        if task is None:
            await self._session.rollback()
            return None
        task.status = TaskStatus.RUNNING
        task.stage = stage
        claimed = ClaimedTask(id=task.id, title=task.title, description=task.description)
        await self._session.commit()
        return claimed


def _require_stage(stage: str) -> None:
    if not isinstance(stage, str) or stage.strip() == "" or len(stage) > STAGE_MAX_LENGTH:
        raise ValueError("stage must be a short non-blank name")
