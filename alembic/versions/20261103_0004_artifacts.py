"""versioned artifacts

Revision ID: 0004_artifacts
Revises: 0003_repositories
Create Date: 2026-11-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_artifacts"
down_revision: str | Sequence[str] | None = "0003_repositories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Matches plan_relative_path plus later artifact names that stay relative.
_RELATIVE_PATH = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(length=200), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1 AND version <= 1000", name="ck_artifacts_version"),
        sa.CheckConstraint(
            "artifact_type ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="ck_artifacts_type",
        ),
        sa.CheckConstraint(
            f"relative_path ~ '{_RELATIVE_PATH}'",
            name="ck_artifacts_relative_path",
        ),
        sa.CheckConstraint(
            "relative_path !~ '\\.\\.'",
            name="ck_artifacts_relative_path_no_dotdot",
        ),
        sa.CheckConstraint("checksum ~ '^[0-9a-f]{64}$'", name="ck_artifacts_checksum"),
        sa.CheckConstraint("size > 0 AND size <= 32768", name="ck_artifacts_size"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_artifacts_task_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "artifact_type",
            "version",
            name="uq_artifacts_task_type_version",
        ),
    )
    op.create_index("ix_artifacts_task_type", "artifacts", ["task_id", "artifact_type"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_task_type", table_name="artifacts")
    op.drop_table("artifacts")
