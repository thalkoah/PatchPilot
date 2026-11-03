"""Task request and response DTOs. These are not ORM models."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.db.models import DESCRIPTION_MAX_LENGTH, TITLE_MAX_LENGTH, TaskStatus

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
MAX_PAGE_OFFSET = 10_000


class TaskCreate(BaseModel):
    """Fields a caller may set. Status, stage, and results are server-owned."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    description: str = Field(min_length=1, max_length=DESCRIPTION_MAX_LENGTH)
    repo_id: UUID | None = None


class TaskResponse(BaseModel):
    """Public state of one task."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str
    repo_id: UUID | None
    status: TaskStatus
    stage: str | None
    result_summary: str | None
    result_json: dict[str, Any] | list[Any] | None
    created_at: datetime
    updated_at: datetime


class TaskPage(BaseModel):
    """One page of tasks, newest first. `total` counts the filtered set."""

    items: list[TaskResponse]
    total: int
    limit: int
    offset: int
