"""Approval request and decision DTOs. These are not ORM models."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.db.models import APPROVAL_REASON_MAX_LENGTH, ApprovalStatus


class DecisionCreate(BaseModel):
    """A human answer to one pending approval. The handler does not run the graph."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approval_id: UUID
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, min_length=1, max_length=APPROVAL_REASON_MAX_LENGTH)


class ApprovalResponse(BaseModel):
    """One approval row. Pending rows are waiting for a decision."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    status: ApprovalStatus
    plan_version: int
    reason: str | None
    created_at: datetime
    decided_at: datetime | None


class ApprovalList(BaseModel):
    """Oldest approvals first, including decisions that were already recorded."""

    items: list[ApprovalResponse]
