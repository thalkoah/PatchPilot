"""Review response DTO. The filesystem path and the diff stay out of this object."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.llm.review import ReviewFinding


class ReviewResponse(BaseModel):
    """The reviewer verdict and the policy decision that followed it."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    version: int
    checksum: str
    size: int
    created_at: datetime
    policy: Literal["accept", "repair", "fail"]
    repair_count: int
    summary: str
    action: Literal["accept", "repair", "escalate"]
    findings: list[ReviewFinding] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
