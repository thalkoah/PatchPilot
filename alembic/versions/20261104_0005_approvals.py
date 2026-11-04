"""plan approvals

Revision ID: 0005_approvals
Revises: 0004_artifacts
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_approvals"
down_revision: str | Sequence[str] | None = "0004_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Matches APPROVAL_REASON_MAX_LENGTH at the time of this revision.
_REASON_MAX = 300

approval_status = postgresql.ENUM(
    "pending",
    "approved",
    "rejected",
    name="approval_status",
)


def upgrade() -> None:
    approval_status.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="approval_status", create_type=False),
            nullable=False,
        ),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=_REASON_MAX), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "plan_version >= 1 AND plan_version <= 1000",
            name="ck_approvals_plan_version",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR "
            f"(char_length(reason) <= {_REASON_MAX} AND length(btrim(reason)) > 0)",
            name="ck_approvals_reason",
        ),
        sa.CheckConstraint(
            "(status = 'pending'::approval_status AND decided_at IS NULL AND reason IS NULL) "
            "OR (status <> 'pending'::approval_status AND decided_at IS NOT NULL)",
            name="ck_approvals_decision_state",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_approvals_task_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approvals_task_id", "approvals", ["task_id"])
    op.create_index(
        "uq_approvals_one_pending",
        "approvals",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'::approval_status"),
    )


def downgrade() -> None:
    op.drop_index("uq_approvals_one_pending", table_name="approvals")
    op.drop_index("ix_approvals_task_id", table_name="approvals")
    op.drop_table("approvals")
    postgresql.ENUM(name="approval_status").drop(op.get_bind(), checkfirst=False)
