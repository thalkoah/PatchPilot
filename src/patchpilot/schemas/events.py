"""Event response DTOs. These are not ORM models."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EventResponse(BaseModel):
    """One append-only audit row. The idempotency key stays internal."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    run_id: UUID | None
    timestamp: datetime
    stage: str
    event_type: str
    payload: dict[str, Any]


class EventList(BaseModel):
    """Oldest events first. `truncated` means the cap hid later rows."""

    items: list[EventResponse]
    truncated: bool
