"""Business tables. LangGraph checkpoints stay out of this metadata."""

import enum
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from patchpilot.db.base import Base

TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 20_000
STAGE_MAX_LENGTH = 64
WORKER_ID_MAX_LENGTH = 64
ERROR_CODE_MAX_LENGTH = 64
EVENT_TYPE_MAX_LENGTH = 64
IDEMPOTENCY_KEY_MAX_LENGTH = 200


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


class TaskRun(Base):
    """One worker attempt. An open run is the lease that keeps other workers away."""

    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt", name="uq_task_runs_task_attempt"),
        CheckConstraint("attempt >= 1", name="ck_task_runs_attempt_positive"),
        Index(
            "uq_task_runs_one_open",
            "task_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_task_runs_open_lease",
            "lease_until",
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tasks.id", name="fk_task_runs_task_id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(WORKER_ID_MAX_LENGTH), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_MAX_LENGTH), nullable=True)

    def __repr__(self) -> str:
        return f"TaskRun(id={self.id!s}, task_id={self.task_id!s}, attempt={self.attempt})"


class TaskEvent(Base):
    """Append-only audit row. Updates and deletes are rejected in the database."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_events_idempotency_key"),
        CheckConstraint("length(btrim(stage)) > 0", name="ck_events_stage_not_blank"),
        CheckConstraint("length(btrim(event_type)) > 0", name="ck_events_event_type_not_blank"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_events_payload_object"),
        Index("ix_events_task_timestamp", "task_id", "timestamp"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tasks.id", name="fk_events_task_id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("task_runs.id", name="fk_events_run_id", ondelete="SET NULL"),
        nullable=True,
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stage: Mapped[str] = mapped_column(String(STAGE_MAX_LENGTH), nullable=False)
    event_type: Mapped[str] = mapped_column(String(EVENT_TYPE_MAX_LENGTH), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_MAX_LENGTH), nullable=False)

    def __repr__(self) -> str:
        return f"TaskEvent(id={self.id!s}, event_type={self.event_type})"
