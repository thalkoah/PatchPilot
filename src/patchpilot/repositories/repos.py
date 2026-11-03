"""PostgreSQL persistence for registered checkouts."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.db.models import Repository


class RepoRepository:
    """Reads and writes `repositories`. Mutating methods commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, repo_id: UUID) -> Repository | None:
        return await self._session.get(Repository, repo_id)

    async def list_all(self) -> list[Repository]:
        rows = await self._session.scalars(
            select(Repository).order_by(Repository.display_name.asc(), Repository.id.asc())
        )
        return list(rows)

    async def save(
        self,
        *,
        display_name: str,
        canonical_path: str,
        registration: dict[str, Any],
    ) -> Repository:
        """Insert a checkout or refresh the existing row for the same path."""
        existing = await self._session.scalar(
            select(Repository).where(Repository.canonical_path == canonical_path)
        )
        if existing is None:
            row = Repository(
                display_name=display_name,
                canonical_path=canonical_path,
                enabled=True,
                registration=registration,
            )
            self._session.add(row)
        else:
            existing.display_name = display_name
            existing.enabled = True
            existing.registration = registration
            row = existing
        await self._session.commit()
        await self._session.refresh(row)
        return row
