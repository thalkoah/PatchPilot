"""Publication DTO. The token is not a field."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PublicationResponse(BaseModel):
    """One draft pull request recorded for a task."""

    model_config = ConfigDict(from_attributes=True)

    task_id: UUID
    owner: str
    name: str
    branch: str
    base_branch: str
    head_sha: str
    pr_number: int
    html_url: str
    draft: bool
