"""publication approvals and draft pull requests

Revision ID: 0008_publications
Revises: 0007_model_calls
Create Date: 2026-11-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_publications"
down_revision: str | Sequence[str] | None = "0007_model_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("kind", sa.String(length=16), server_default="plan", nullable=False),
    )
    op.create_check_constraint(
        "ck_approvals_kind",
        "approvals",
        "kind IN ('plan', 'publish')",
    )
    op.create_table(
        "publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("branch", sa.String(length=80), nullable=False),
        sa.Column("base_branch", sa.String(length=100), nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("html_url", sa.String(length=300), nullable=False),
        sa.Column("draft", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "owner ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'",
            name="ck_publications_owner",
        ),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'",
            name="ck_publications_name",
        ),
        sa.CheckConstraint(
            "branch = 'patchpilot/' || task_id::text",
            name="ck_publications_branch",
        ),
        sa.CheckConstraint(
            "base_branch ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$' "
            "AND base_branch !~ '\\.\\.' "
            "AND base_branch <> branch",
            name="ck_publications_base_branch",
        ),
        sa.CheckConstraint(
            "head_sha ~ '^[0-9a-f]{40}$'",
            name="ck_publications_head_sha",
        ),
        sa.CheckConstraint(
            "pr_number >= 1 AND pr_number <= 1000000000",
            name="ck_publications_pr_number",
        ),
        sa.CheckConstraint("draft IS TRUE", name="ck_publications_draft"),
        sa.CheckConstraint(
            "html_url = 'https://github.com/' || owner || '/' || name || '/pull/' "
            "|| pr_number::text",
            name="ck_publications_html_url",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_publications_task_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name="fk_publications_approval_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_publications_task_id"),
    )


def downgrade() -> None:
    op.drop_table("publications")
    op.drop_constraint("ck_approvals_kind", "approvals", type_="check")
    op.drop_column("approvals", "kind")
