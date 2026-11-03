"""task runs and append-only events

Revision ID: 0002_task_runs_and_events
Revises: 0001_create_tasks
Create Date: 2026-11-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_task_runs_and_events"
down_revision: str | Sequence[str] | None = "0001_create_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint("attempt >= 1", name="ck_task_runs_attempt_positive"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_runs_task_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "attempt", name="uq_task_runs_task_attempt"),
    )
    op.create_index(
        "uq_task_runs_one_open",
        "task_runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_task_runs_open_lease",
        "task_runs",
        ["lease_until"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.CheckConstraint("length(btrim(stage)) > 0", name="ck_events_stage_not_blank"),
        sa.CheckConstraint(
            "length(btrim(event_type)) > 0",
            name="ck_events_event_type_not_blank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_events_payload_object",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_events_task_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["task_runs.id"],
            name="fk_events_run_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_events_idempotency_key"),
    )
    op.create_index("ix_events_task_timestamp", "events", ["task_id", "timestamp"])
    op.execute(
        """
        CREATE FUNCTION patchpilot_reject_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'events are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER events_append_only
        BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW
        EXECUTE FUNCTION patchpilot_reject_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_append_only ON events")
    op.drop_index("ix_events_task_timestamp", table_name="events")
    op.drop_table("events")
    op.execute("DROP FUNCTION IF EXISTS patchpilot_reject_event_mutation()")
    op.drop_index("ix_task_runs_open_lease", table_name="task_runs")
    op.drop_index("uq_task_runs_one_open", table_name="task_runs")
    op.drop_table("task_runs")
