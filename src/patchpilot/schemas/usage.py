"""Usage read model. Cost is unknown unless a stored row says it was estimated."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.agents.limits import DEFAULT_MAX_REPAIRS
from patchpilot.db.models import ModelCall
from patchpilot.llm.budget import (
    DEFAULT_MAX_MODEL_CALLS,
    DEFAULT_MAX_RUNTIME_SECONDS,
    DEFAULT_MAX_TASK_TOOL_CALLS,
    DEFAULT_MAX_TOKENS,
)

CostState = Literal["estimated", "unknown"]


class UsageLimits(BaseModel):
    """The ceilings the worker enforces. They are not a bill."""

    model_config = ConfigDict(extra="forbid")

    model_calls: int = DEFAULT_MAX_MODEL_CALLS
    tool_calls: int = DEFAULT_MAX_TASK_TOOL_CALLS
    tokens: int = DEFAULT_MAX_TOKENS
    runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS
    parallelism: int = 1
    repairs: int = DEFAULT_MAX_REPAIRS


class UsageCall(BaseModel):
    """One stored call. Token fields are null when the provider did not report them."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    stage: str
    provider: str
    model: str
    prompt_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    cost: CostState
    estimated_cost_usd: str | None
    outcome: str
    created_at: datetime


class UsageTotals(BaseModel):
    """Sums that are complete. A missing token or price keeps the total unknown."""

    model_config = ConfigDict(extra="forbid")

    model_calls: int
    prompt_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    cost: CostState
    estimated_cost_usd: str | None


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    calls: list[UsageCall]
    totals: UsageTotals
    limits: UsageLimits = Field(default_factory=UsageLimits)


def usage_response(task_id: UUID, rows: list[ModelCall], limits: UsageLimits) -> UsageResponse:
    calls = [_call(row) for row in rows]
    return UsageResponse(
        task_id=task_id,
        calls=calls,
        totals=_totals(calls),
        limits=limits,
    )


def _call(row: ModelCall) -> UsageCall:
    amount = row.estimated_cost_usd
    if row.cost_known and amount is not None:
        cost = "estimated"
        estimated = _money(amount)
    else:
        cost = "unknown"
        estimated = None
    return UsageCall(
        id=row.id,
        stage=row.stage,
        provider=row.provider,
        model=row.model,
        prompt_tokens=row.prompt_tokens,
        output_tokens=row.output_tokens,
        latency_ms=row.latency_ms,
        cost=cost,
        estimated_cost_usd=estimated,
        outcome=row.outcome,
        created_at=row.created_at,
    )


def _totals(calls: list[UsageCall]) -> UsageTotals:
    prompt_known = bool(calls) and all(item.prompt_tokens is not None for item in calls)
    output_known = bool(calls) and all(item.output_tokens is not None for item in calls)
    cost_known = bool(calls) and all(item.cost == "estimated" for item in calls)
    cost = Decimal(0)
    if cost_known:
        for item in calls:
            if item.estimated_cost_usd is not None:
                cost += Decimal(item.estimated_cost_usd)
    return UsageTotals(
        model_calls=len(calls),
        prompt_tokens=sum(item.prompt_tokens or 0 for item in calls) if prompt_known else None,
        output_tokens=sum(item.output_tokens or 0 for item in calls) if output_known else None,
        latency_ms=sum(item.latency_ms for item in calls),
        cost="estimated" if cost_known else "unknown",
        estimated_cost_usd=_money(cost) if cost_known else None,
    )


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001'))}"
