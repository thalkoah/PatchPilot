"""HTML pages. Create and decide by calling the task API; this router does not write."""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from patchpilot.config import get_settings
from patchpilot.dependencies import (
    get_approval_repository,
    get_artifact_repository,
    get_event_repository,
    get_model_call_repository,
    get_repo_repository,
    get_task_repository,
)
from patchpilot.repositories.approvals import ApprovalRepository
from patchpilot.repositories.artifacts import ArtifactRepository
from patchpilot.repositories.events import EventRepository
from patchpilot.repositories.model_calls import ModelCallRepository
from patchpilot.repositories.repos import RepoRepository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.schemas.tasks import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    MAX_PAGE_OFFSET,
    TaskStatus,
)
from patchpilot.web.load import LoadedTask, load_task
from patchpilot.web.present import index_page, progress_state, select_new_events, task_page
from patchpilot.web.redact import secret_markers

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

router = APIRouter(include_in_schema=False, default_response_class=HTMLResponse)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

TaskDep = Annotated[TaskRepository, Depends(get_task_repository)]
EventDep = Annotated[EventRepository, Depends(get_event_repository)]
ArtifactDep = Annotated[ArtifactRepository, Depends(get_artifact_repository)]
ApprovalDep = Annotated[ApprovalRepository, Depends(get_approval_repository)]
CallDep = Annotated[ModelCallRepository, Depends(get_model_call_repository)]
RepoDep = Annotated[RepoRepository, Depends(get_repo_repository)]


def _render(
    request: Request,
    name: str,
    context: dict[str, object],
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        name,
        context,
        status_code=status_code,
        headers=_HEADERS,
    )


@router.get("/")
async def dashboard_index(
    request: Request,
    tasks: TaskDep,
    repos: RepoDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_PAGE_OFFSET)] = 0,
    status: TaskStatus | None = None,
) -> HTMLResponse:
    """Task list and the create form. The form posts JSON to the task API."""
    rows, total = await tasks.list_tasks(status=status, limit=limit, offset=offset)
    enabled = [(str(row.id), row.display_name) for row in await repos.list_all() if row.enabled]
    page = index_page(
        rows,
        total=total,
        limit=limit,
        offset=offset,
        status=status,
        repositories=enabled,
        markers=secret_markers(get_settings()),
    )
    return _render(request, "index.html", {"page": page})


@router.get("/tasks/{task_id}")
async def dashboard_task(
    request: Request,
    task_id: UUID,
    tasks: TaskDep,
    events: EventDep,
    artifacts: ArtifactDep,
    approvals: ApprovalDep,
    calls: CallDep,
) -> HTMLResponse:
    """One task. Approval buttons call the existing decisions endpoint."""
    loaded = await _loaded(task_id, tasks, events, artifacts, approvals, calls)
    if loaded is None:
        return _missing(request)
    page = task_page(loaded, secret_markers(get_settings()))
    return _render(request, "task.html", {"page": page})


@router.get("/tasks/{task_id}/progress")
async def dashboard_progress(
    request: Request,
    task_id: UUID,
    tasks: TaskDep,
    events: EventDep,
    after: UUID | None = None,
) -> HTMLResponse:
    """New timeline rows after a cursor. The same event id is not returned again."""
    task = await tasks.get(task_id)
    if task is None:
        return _missing(request)
    rows, _truncated = await events.list_for_task(task_id)
    status, stage, summary, shown = progress_state(task, rows, secret_markers(get_settings()))
    fresh = select_new_events(shown, None if after is None else str(after))
    return _render(
        request,
        "progress.html",
        {"status": status, "stage": stage, "summary": summary, "events": fresh},
    )


async def _loaded(
    task_id: UUID,
    tasks: TaskRepository,
    events: EventRepository,
    artifacts: ArtifactRepository,
    approvals: ApprovalRepository,
    calls: ModelCallRepository,
) -> LoadedTask | None:
    return await load_task(
        tasks=tasks,
        events=events,
        artifacts=artifacts,
        approvals=approvals,
        calls=calls,
        settings=get_settings(),
        task_id=task_id,
    )


def _missing(request: Request) -> HTMLResponse:
    return _render(request, "missing.html", {}, status_code=404)
