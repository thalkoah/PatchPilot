"""isolated git workspaces

Revision ID: 0006_workspaces
Revises: 0005_approvals
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_workspaces"
down_revision: str | Sequence[str] | None = "0005_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

workspace_status = postgresql.ENUM(
    "active",
    "removed",
    name="workspace_status",
)


def upgrade() -> None:
    workspace_status.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_sha", sa.String(length=40), nullable=False),
        sa.Column("branch", sa.String(length=64), nullable=False),
        sa.Column("clone_path", sa.String(length=80), nullable=False),
        sa.Column("worktree_path", sa.String(length=80), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="workspace_status", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("base_sha ~ '^[0-9a-f]{40}$'", name="ck_workspaces_base_sha"),
        sa.CheckConstraint(
            "branch = 'patchpilot/' || task_id::text",
            name="ck_workspaces_branch",
        ),
        sa.CheckConstraint(
            "clone_path = task_id::text || '/clone'",
            name="ck_workspaces_clone_path",
        ),
        sa.CheckConstraint(
            "worktree_path = task_id::text || '/worktree'",
            name="ck_workspaces_worktree_path",
        ),
        sa.CheckConstraint(
            "(status = 'active'::workspace_status AND removed_at IS NULL) "
            "OR (status = 'removed'::workspace_status AND removed_at IS NOT NULL)",
            name="ck_workspaces_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_workspaces_task_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repositories.id"],
            name="fk_workspaces_repo_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_workspaces_task_id"),
    )
    op.create_index("ix_workspaces_repo_id", "workspaces", ["repo_id"])


def downgrade() -> None:
    op.drop_index("ix_workspaces_repo_id", table_name="workspaces")
    op.drop_table("workspaces")
    postgresql.ENUM(name="workspace_status").drop(op.get_bind(), checkfirst=False)
