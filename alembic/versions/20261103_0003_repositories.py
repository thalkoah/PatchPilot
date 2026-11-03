"""repositories and task repo foreign key

Revision ID: 0003_repositories
Revises: 0002_task_runs_and_events
Create Date: 2026-11-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_repositories"
down_revision: str | Sequence[str] | None = "0002_task_runs_and_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Lengths match DISPLAY_NAME_MAX_LENGTH (200) and CANONICAL_PATH_MAX_LENGTH (512).
_CANONICAL_PATH = r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,63})*$"


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("canonical_path", sa.String(length=512), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "registration",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "length(btrim(display_name)) > 0",
            name="ck_repositories_display_name_not_blank",
        ),
        sa.CheckConstraint(
            "char_length(display_name) <= 200",
            name="ck_repositories_display_name_length",
        ),
        sa.CheckConstraint(
            f"canonical_path ~ '{_CANONICAL_PATH}'",
            name="ck_repositories_canonical_path",
        ),
        sa.CheckConstraint(
            "char_length(canonical_path) <= 512",
            name="ck_repositories_canonical_path_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(registration) = 'object'",
            name="ck_repositories_registration_object",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_path", name="uq_repositories_canonical_path"),
    )
    op.create_index("ix_repositories_enabled", "repositories", ["enabled"])
    op.create_foreign_key(
        "fk_tasks_repo_id",
        "tasks",
        "repositories",
        ["repo_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_repo_id", "tasks", type_="foreignkey")
    op.drop_index("ix_repositories_enabled", table_name="repositories")
    op.drop_table("repositories")
