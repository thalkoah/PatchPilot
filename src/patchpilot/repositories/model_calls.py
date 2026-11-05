"""Stored model calls. Rows are inserted after the call and never updated."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.db.models import ModelCall
from patchpilot.llm.budget import BudgetSeed
from patchpilot.llm.meter import OUTCOME_INVALID, OUTCOME_SUCCESS, ModelCallDraft


class ModelCallRepository:
    """Reads and writes `model_calls`. Inserts commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_many(self, records: list[ModelCallDraft]) -> list[ModelCall]:
        rows = [_row(record) for record in records]
        self._session.add_all(rows)
        await self._session.commit()
        return rows

    async def list_for_task(self, task_id: UUID) -> list[ModelCall]:
        result = await self._session.scalars(
            select(ModelCall)
            .where(ModelCall.task_id == task_id)
            .order_by(ModelCall.created_at, ModelCall.id)
        )
        return list(result.all())

    async def seed(self, task_id: UUID) -> BudgetSeed:
        """Counts already stored, so a resumed task does not get a fresh budget."""
        rows = await self.list_for_task(task_id)
        prompt = 0
        output = 0
        reported = False
        streak: dict[tuple[str, str, str], int] = {}
        for row in rows:
            if row.prompt_tokens is not None:
                reported = True
                prompt += row.prompt_tokens
            if row.output_tokens is not None:
                reported = True
                output += row.output_tokens
            key = (row.stage, row.provider, row.model)
            if row.outcome == OUTCOME_INVALID:
                streak[key] = streak.get(key, 0) + 1
            elif row.outcome == OUTCOME_SUCCESS:
                streak[key] = 0
        return BudgetSeed(
            model_calls=len(rows),
            prompt_tokens=prompt,
            output_tokens=output,
            tokens_reported=reported,
            invalid_streak=streak,
        )


def _row(record: ModelCallDraft) -> ModelCall:
    cost: Decimal | None = record.estimated_cost_usd
    return ModelCall(
        task_id=record.task_id,
        stage=record.stage,
        provider=record.provider,
        model=record.model,
        prompt_tokens=record.prompt_tokens,
        output_tokens=record.output_tokens,
        latency_ms=record.latency_ms,
        estimated_cost_usd=cost,
        cost_known=record.cost_known,
        outcome=record.outcome,
    )
