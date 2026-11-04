"""Task API handlers. They validate DTOs and call the repository."""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from patchpilot.artifacts import (
    PLAN_SCHEMA_VERSION,
    ArtifactStoreError,
    load_diff_file,
    load_plan_file,
    load_proposal_file,
)
from patchpilot.config import get_settings
from patchpilot.dependencies import (
    get_approval_repository,
    get_artifact_repository,
    get_event_repository,
    get_task_repository,
)
from patchpilot.repositories.approvals import (
    ApprovalConflict,
    ApprovalNotFound,
    ApprovalRepository,
)
from patchpilot.repositories.artifacts import ArtifactRepository
from patchpilot.repositories.events import EventRepository
from patchpilot.repositories.tasks import TaskRepository, UnregisteredRepositoryError
from patchpilot.schemas.approvals import ApprovalList, ApprovalResponse, DecisionCreate
from patchpilot.schemas.events import EventList, EventResponse
from patchpilot.schemas.patches import PatchResponse
from patchpilot.schemas.plans import PlanResponse
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
EventRepositoryDep = Annotated[EventRepository, Depends(get_event_repository)]
ArtifactRepositoryDep = Annotated[ArtifactRepository, Depends(get_artifact_repository)]
ApprovalRepositoryDep = Annotated[ApprovalRepository, Depends(get_approval_repository)]


@router.post("", status_code=201, response_model=TaskResponse)
async def create_task(body: TaskCreate, repository: RepositoryDep) -> TaskResponse:
    try:
        task = await repository.create(
            title=body.title,
            description=body.description,
            repo_id=body.repo_id,
        )
    except UnregisteredRepositoryError:
        raise HTTPException(status_code=422, detail="Repository is not registered") from None
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


@router.get("/{task_id}/events", response_model=EventList)
async def list_task_events(
    task_id: UUID,
    repository: RepositoryDep,
    events: EventRepositoryDep,
) -> EventList:
    task = await repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows, truncated = await events.list_for_task(task_id)
    return EventList(
        items=[EventResponse.model_validate(row) for row in rows],
        truncated=truncated,
    )


@router.get("/{task_id}/plan", response_model=PlanResponse)
async def get_task_plan(
    task_id: UUID,
    repository: RepositoryDep,
    artifacts: ArtifactRepositoryDep,
    version: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> PlanResponse:
    """Return one stored plan. The handler does not call a model or write the checkout."""
    task = await repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    row = await artifacts.get_plan(task_id, version)
    root = get_settings().artifacts_root
    if row is None or root is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    try:
        plan = load_plan_file(
            Path(root),
            relative_path=row.relative_path,
            expected_checksum=row.checksum,
            size=row.size,
        )
    except ArtifactStoreError:
        raise HTTPException(status_code=404, detail="Plan not found") from None
    return PlanResponse(
        task_id=task_id,
        version=row.version,
        schema_version=PLAN_SCHEMA_VERSION,
        checksum=row.checksum,
        size=row.size,
        created_at=row.created_at,
        plan=plan,
    )


@router.get("/{task_id}/patch", response_model=PatchResponse)
async def get_task_patch(
    task_id: UUID,
    repository: RepositoryDep,
    artifacts: ArtifactRepositoryDep,
    version: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> PatchResponse:
    """Return the applied diff and the model proposal. Neither file is executed."""
    task = await repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    diff_row = await artifacts.get_diff(task_id, version)
    root = get_settings().artifacts_root
    if diff_row is None or root is None:
        raise HTTPException(status_code=404, detail="Patch not found")
    proposal_row = await artifacts.get_proposal(task_id, diff_row.version)
    if proposal_row is None:
        raise HTTPException(status_code=404, detail="Patch not found")
    try:
        diff = load_diff_file(
            Path(root),
            relative_path=diff_row.relative_path,
            expected_checksum=diff_row.checksum,
            size=diff_row.size,
        )
        proposal = load_proposal_file(
            Path(root),
            relative_path=proposal_row.relative_path,
            expected_checksum=proposal_row.checksum,
            size=proposal_row.size,
        )
    except ArtifactStoreError:
        raise HTTPException(status_code=404, detail="Patch not found") from None
    text = diff.decode("utf-8")
    paths = _patch_paths(text)
    if not paths:
        raise HTTPException(status_code=404, detail="Patch not found")
    return PatchResponse(
        task_id=task_id,
        version=diff_row.version,
        checksum=diff_row.checksum,
        proposal_checksum=proposal_row.checksum,
        size=diff_row.size,
        created_at=diff_row.created_at,
        summary=proposal.summary,
        paths=paths,
        diff=text,
        proposal=proposal,
    )


def _patch_paths(diff: str) -> list[str]:
    paths: list[str] = []
    marker = "diff --git a/"
    for line in diff.splitlines():
        if not line.startswith(marker) or " b/" not in line:
            continue
        left, right = line[len(marker) :].split(" b/", 1)
        if left == right and left and left not in paths:
            paths.append(left)
    return paths


@router.get("/{task_id}/approvals", response_model=ApprovalList)
async def list_task_approvals(
    task_id: UUID,
    repository: RepositoryDep,
    approvals: ApprovalRepositoryDep,
) -> ApprovalList:
    """Return approval requests for one task. Pending rows are still undecided."""
    task = await repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = await approvals.list_for_task(task_id)
    return ApprovalList(items=[ApprovalResponse.model_validate(row) for row in rows])


@router.post("/{task_id}/decisions", response_model=ApprovalResponse)
async def decide_task(
    task_id: UUID,
    body: DecisionCreate,
    repository: RepositoryDep,
    approvals: ApprovalRepositoryDep,
) -> ApprovalResponse:
    """Record one approval decision and queue the same thread to resume.

    The handler does not call a model and does not write a repository checkout.
    Repeating the stored decision returns that row and does not queue another run.
    """
    task = await repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        view = await approvals.decide(
            task_id,
            body.approval_id,
            body.decision,
            body.reason,
        )
    except ApprovalNotFound:
        raise HTTPException(status_code=404, detail="Approval not found") from None
    except ApprovalConflict:
        raise HTTPException(
            status_code=409,
            detail="Approval already has a different decision",
        ) from None
    return ApprovalResponse.model_validate(view)
