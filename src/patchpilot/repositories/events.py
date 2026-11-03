"""Append-only event writes and the read path for the audit API."""

from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.audit import MAX_EVENT_PAGE, bound_payload, require_event_type, require_stage
from patchpilot.db.models import TaskEvent


class EventRepository:
    """Reads the audit trail. Writes go through `append_event` inside a larger transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_task(self, task_id: UUID) -> tuple[list[TaskEvent], bool]:
        """Return the oldest events, capped, plus whether anything was left out."""
        statement = (
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.timestamp.asc(), TaskEvent.id.asc())
            .limit(MAX_EVENT_PAGE + 1)
        )
        rows = list(await self._session.scalars(statement))
        truncated = len(rows) > MAX_EVENT_PAGE
        return rows[:MAX_EVENT_PAGE], truncated


async def append_event(
    session: AsyncSession,
    *,
    task_id: UUID,
    run_id: UUID | None,
    stage: str,
    event_type: str,
    payload: dict[str, object],
    idempotency_key: str,
    ordinal: int,
) -> None:
    """Insert one audit row. The same idempotency key does not change the stored payload.

    The caller commits. `ordinal` separates rows written in one transaction so the
    API can return them in the order they were recorded.
    """
    checked_stage = require_stage(stage)
    checked_type = require_event_type(event_type)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0 or ordinal > 20:
        raise ValueError("event ordinal must be a small non-negative integer")
    statement = pg_insert(TaskEvent).values(
        id=uuid4(),
        task_id=task_id,
        run_id=run_id,
        timestamp=func.clock_timestamp()
        + func.make_interval(0, 0, 0, 0, 0, 0, ordinal / 1_000_000),
        stage=checked_stage,
        event_type=checked_type,
        payload=bound_payload(payload),
        idempotency_key=idempotency_key,
    )
    await session.execute(statement.on_conflict_do_nothing(constraint="uq_events_idempotency_key"))
