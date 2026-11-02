"""create tasks table

Revision ID: 0001_create_tasks
Revises:
Create Date: 2026-11-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_create_tasks"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Lengths match TITLE_MAX_LENGTH (200) and DESCRIPTION_MAX_LENGTH (20000)
# at the time of this revision. Later changes need a new migration.
task_status = postgresql.ENUM(
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "rejected",
    "failed",
    "cancelled",
    name="task_status",
)


def upgrade() -> None:
    task_status.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="task_status", create_type=False),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_tasks_title_not_blank"),
        sa.CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_tasks_description_not_blank",
        ),
        sa.CheckConstraint(
            "char_length(description) <= 20000",
            name="ck_tasks_description_length",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_status", "tasks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_table("tasks")
    task_status.drop(op.get_bind(), checkfirst=False)
