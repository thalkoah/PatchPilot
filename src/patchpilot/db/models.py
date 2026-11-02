"""Business tables. LangGraph checkpoints stay out of this metadata."""

import enum
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from patchpilot.db.base import Base

TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 20_000
STAGE_MAX_LENGTH = 64


class TaskStatus(enum.StrEnum):
    """Lifecycle of a bug-report task.

    `rejected` is included now so a later approval flow does not have to alter
    the PostgreSQL type. New tasks are always inserted as `queued`.
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _enum_values(members: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in members]


task_status_type = ENUM(
    TaskStatus,
    name="task_status",
    values_callable=_enum_values,
    create_type=False,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Task(Base):
    """A bug report accepted by the API. Workers fill stage and result later."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("length(btrim(title)) > 0", name="ck_tasks_title_not_blank"),
        CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_tasks_description_not_blank",
        ),
        CheckConstraint(
            f"char_length(description) <= {DESCRIPTION_MAX_LENGTH}",
            name="ck_tasks_description_length",
        ),
        Index("ix_tasks_created_at", "created_at"),
        Index("ix_tasks_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Reserved for the repository registry. Creation leaves this null.
    repo_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(task_status_type, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(STAGE_MAX_LENGTH), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"Task(id={self.id!s}, status={self.status})"
