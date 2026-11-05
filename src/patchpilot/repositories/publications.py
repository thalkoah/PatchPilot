"""One draft pull request per task. A second insert of the same PR is a no-op."""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.db.models import Publication
from patchpilot.github.names import GitHubRepo


class PublicationStoreError(Exception):
    """The publication row could not be stored. Nothing about it was committed."""


class PublicationRepository:
    """Reads the draft recorded for one task."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_task(self, task_id: UUID) -> Publication | None:
        return await self._session.scalar(select(Publication).where(Publication.task_id == task_id))


async def insert_publication(
    session: AsyncSession,
    *,
    task_id: UUID,
    approval_id: UUID,
    owner: str,
    name: str,
    branch: str,
    base_branch: str,
    head_sha: str,
    pr_number: int,
    html_url: str,
) -> Publication:
    """Insert the draft, or return the row when it is the same pull request.

    The caller commits. A different pull request for the same task is refused.
    """
    repo = GitHubRepo(owner=owner, name=name)
    if (
        branch != f"patchpilot/{task_id}"
        or base_branch == branch
        or html_url != repo.pull_url(pr_number)
        or not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
    ):
        raise PublicationStoreError
    existing = await session.scalar(select(Publication).where(Publication.task_id == task_id))
    if existing is not None:
        if existing.pr_number == pr_number and existing.html_url == html_url:
            return existing
        raise PublicationStoreError
    row = Publication(
        id=uuid4(),
        task_id=task_id,
        approval_id=approval_id,
        owner=owner,
        name=name,
        branch=branch,
        base_branch=base_branch,
        head_sha=head_sha,
        pr_number=pr_number,
        html_url=html_url,
        draft=True,
    )
    session.add(row)
    await session.flush()
    return row
