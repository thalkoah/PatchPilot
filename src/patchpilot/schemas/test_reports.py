"""Test report response DTO. The filesystem path stays internal."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TestReportResponse(BaseModel):
    """The pytest result captured by the restricted runner."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    version: int
    checksum: str
    size: int
    created_at: datetime
    status: Literal["passed", "failed", "timed_out"]
    passed: bool
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    output_truncated: bool
