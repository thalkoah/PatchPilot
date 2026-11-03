"""Versioned artifact rows. Plan bytes live on the artifact volume."""

from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.artifacts import (
    MAX_PLAN_VERSION,
    PLAN_ARTIFACT_TYPE,
    ArtifactStoreError,
    checksum,
    plan_document,
    plan_relative_path,
    prepare_artifact_root,
    write_plan_file,
)
from patchpilot.db.models import Artifact
from patchpilot.llm.plan import ChangePlan


class ArtifactRepository:
    """Reads artifact metadata. The route loads the file after this returns a row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_plan(self, task_id: UUID, version: int | None = None) -> Artifact | None:
        """Return one plan revision, or the newest when `version` is omitted."""
        statement = select(Artifact).where(
            Artifact.task_id == task_id,
            Artifact.artifact_type == PLAN_ARTIFACT_TYPE,
        )
        if version is None:
            statement = statement.order_by(Artifact.version.desc()).limit(1)
        else:
            statement = statement.where(Artifact.version == version)
        return await self._session.scalar(statement)


async def persist_plan(
    session: AsyncSession,
    root: Path,
    task_id: UUID,
    plan: ChangePlan,
) -> int:
    """Store a plan revision and return its version. The caller commits.

    The same bytes for this task reuse the existing version. A different plan
    becomes the next version. This function does not write the checkout.
    """
    if not isinstance(root, Path):
        raise ArtifactStoreError
    payload = plan_document(plan)
    digest = checksum(payload)
    existing = await session.scalar(
        select(Artifact).where(
            Artifact.task_id == task_id,
            Artifact.artifact_type == PLAN_ARTIFACT_TYPE,
            Artifact.checksum == digest,
        )
    )
    if existing is not None:
        return existing.version
    current = await session.scalar(
        select(func.max(Artifact.version)).where(
            Artifact.task_id == task_id,
            Artifact.artifact_type == PLAN_ARTIFACT_TYPE,
        )
    )
    version = int(current or 0) + 1
    if version > MAX_PLAN_VERSION:
        raise ArtifactStoreError
    relative = plan_relative_path(task_id, version)
    write_plan_file(prepare_artifact_root(root), relative, payload)
    session.add(
        Artifact(
            id=uuid4(),
            task_id=task_id,
            artifact_type=PLAN_ARTIFACT_TYPE,
            version=version,
            relative_path=relative,
            checksum=digest,
            size=len(payload),
        )
    )
    await session.flush()
    return version
