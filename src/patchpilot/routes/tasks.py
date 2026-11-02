"""Task API handlers. They validate DTOs and call the repository."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from patchpilot.dependencies import get_task_repository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.schemas.tasks import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    MAX_PAGE_OFFSET,
    TaskCreate,
    TaskPage,
    TaskResponse,
    TaskStatus,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])
RepositoryDep = Annotated[TaskRepository, Depends(get_task_repository)]


@router.post("", status_code=201, response_model=TaskResponse)
async def create_task(body: TaskCreate, repository: RepositoryDep) -> TaskResponse:
    task = await repository.create(title=body.title, description=body.description)
    return TaskResponse.model_validate(task)


@router.get("", response_model=TaskPage)
async def list_tasks(
    repository: RepositoryDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_PAGE_OFFSET)] = 0,
    status: TaskStatus | None = None,
) -> TaskPage:
    tasks, total = await repository.list_tasks(status=status, limit=limit, offset=offset)
    return TaskPage(
        items=[TaskResponse.model_validate(task) for task in tasks],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: UUID, repository: RepositoryDep) -> TaskResponse:
    task = await repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.model_validate(task)
