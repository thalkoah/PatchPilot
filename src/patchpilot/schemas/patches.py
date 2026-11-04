"""Patch response DTO. The filesystem path stays internal."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.llm.patch import PatchProposal


class PatchResponse(BaseModel):
    """The applied diff and the model proposal that produced it."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    version: int
    checksum: str
    proposal_checksum: str
    size: int
    created_at: datetime
    summary: str
    paths: list[str] = Field(min_length=1)
    diff: str
    proposal: PatchProposal
