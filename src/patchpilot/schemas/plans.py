"""Plan response DTO. The filesystem path stays internal."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from patchpilot.artifacts import PLAN_SCHEMA_VERSION
from patchpilot.llm.plan import ChangePlan


class PlanResponse(BaseModel):
    """The newest plan, or one numbered revision, read back from its artifact."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    version: int
    schema_version: int = PLAN_SCHEMA_VERSION
    checksum: str
    size: int
    created_at: datetime
    plan: ChangePlan
