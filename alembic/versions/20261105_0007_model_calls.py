"""model call usage

Revision ID: 0007_model_calls
Revises: 0006_workspaces
Create Date: 2026-11-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_model_calls"
down_revision: str | Sequence[str] | None = "0006_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTCOMES = (
    "success",
    "transient_error",
    "permanent_error",
    "invalid_output",
    "budget_exhausted",
    "circuit_open",
    "capability_rejected",
)
_STAGES = ("triage", "investigate", "plan", "develop", "review", "supervisor")


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("cost_known", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('fake', 'ollama', 'openai')",
            name="ck_model_calls_provider",
        ),
        sa.CheckConstraint(
            "stage IN (" + ", ".join(f"'{stage}'" for stage in _STAGES) + ")",
            name="ck_model_calls_stage",
        ),
        sa.CheckConstraint(
            "outcome IN (" + ", ".join(f"'{item}'" for item in _OUTCOMES) + ")",
            name="ck_model_calls_outcome",
        ),
        sa.CheckConstraint("latency_ms >= 0", name="ck_model_calls_latency"),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_model_calls_prompt_tokens",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_model_calls_output_tokens",
        ),
        sa.CheckConstraint(
            "(cost_known = false AND estimated_cost_usd IS NULL) OR "
            "(cost_known = true AND estimated_cost_usd IS NOT NULL "
            "AND estimated_cost_usd >= 0)",
            name="ck_model_calls_cost",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_model_calls_task_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_calls_task_created",
        "model_calls",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_calls_task_created", table_name="model_calls")
    op.drop_table("model_calls")
