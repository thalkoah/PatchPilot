"""Versioned artifact rows. File bytes live on the artifact volume."""

from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.artifacts import (
    DIFF_ARTIFACT_TYPE,
    MAX_PLAN_VERSION,
    PLAN_ARTIFACT_TYPE,
    PROPOSAL_ARTIFACT_TYPE,
    REVIEW_ARTIFACT_TYPE,
    TEST_REPORT_ARTIFACT_TYPE,
    ArtifactStoreError,
    TestReportStoreError,
    checksum,
    diff_relative_path,
    plan_document,
    plan_relative_path,
    prepare_artifact_root,
    proposal_document,
    proposal_relative_path,
    review_document,
    review_relative_path,
    test_report_document,
    test_report_relative_path,
    write_diff_file,
    write_plan_file,
    write_proposal_file,
    write_review_file,
    write_test_report_file,
)
from patchpilot.db.models import Artifact
from patchpilot.llm.patch import PatchProposal
from patchpilot.llm.plan import ChangePlan
from patchpilot.llm.review import ReviewResult
from patchpilot.runner.protocol import TestReport


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

    async def get_diff(self, task_id: UUID, version: int | None = None) -> Artifact | None:
        """Return one applied diff, or the newest when `version` is omitted."""
        return await self._get(task_id, DIFF_ARTIFACT_TYPE, version)

    async def get_proposal(self, task_id: UUID, version: int | None = None) -> Artifact | None:
        """Return one model proposal, or the newest when `version` is omitted."""
        return await self._get(task_id, PROPOSAL_ARTIFACT_TYPE, version)

    async def get_test_report(self, task_id: UUID, version: int | None = None) -> Artifact | None:
        """Return one test report, or the newest when `version` is omitted."""
        return await self._get(task_id, TEST_REPORT_ARTIFACT_TYPE, version)

    async def get_review(self, task_id: UUID, version: int | None = None) -> Artifact | None:
        """Return one review, or the newest when `version` is omitted."""
        return await self._get(task_id, REVIEW_ARTIFACT_TYPE, version)

    async def _get(self, task_id: UUID, artifact_type: str, version: int | None) -> Artifact | None:
        statement = select(Artifact).where(
            Artifact.task_id == task_id,
            Artifact.artifact_type == artifact_type,
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


async def persist_patch(
    session: AsyncSession,
    root: Path,
    task_id: UUID,
    proposal: PatchProposal,
    diff: bytes,
) -> int:
    """Store the model proposal and the applied diff under one version.

    The same diff bytes reuse the existing version. The proposal file is a
    different document, so its checksum is not the diff checksum. The caller
    commits. This function does not write the registered checkout.
    """
    if not isinstance(root, Path) or not isinstance(diff, bytes) or not diff:
        raise ArtifactStoreError
    proposal_payload = proposal_document(proposal)
    diff_digest = checksum(diff)
    existing = await session.scalar(
        select(Artifact).where(
            Artifact.task_id == task_id,
            Artifact.artifact_type == DIFF_ARTIFACT_TYPE,
            Artifact.checksum == diff_digest,
        )
    )
    if existing is not None:
        return existing.version
    current = await session.scalar(
        select(func.max(Artifact.version)).where(
            Artifact.task_id == task_id,
            Artifact.artifact_type == DIFF_ARTIFACT_TYPE,
        )
    )
    version = int(current or 0) + 1
    if version > MAX_PLAN_VERSION:
        raise ArtifactStoreError
    base = prepare_artifact_root(root)
    diff_path = diff_relative_path(task_id, version)
    proposal_path = proposal_relative_path(task_id, version)
    write_diff_file(base, diff_path, diff)
    write_proposal_file(base, proposal_path, proposal_payload)
    session.add(
        Artifact(
            id=uuid4(),
            task_id=task_id,
            artifact_type=DIFF_ARTIFACT_TYPE,
            version=version,
            relative_path=diff_path,
            checksum=diff_digest,
            size=len(diff),
        )
    )
    session.add(
        Artifact(
            id=uuid4(),
            task_id=task_id,
            artifact_type=PROPOSAL_ARTIFACT_TYPE,
            version=version,
            relative_path=proposal_path,
            checksum=checksum(proposal_payload),
            size=len(proposal_payload),
        )
    )
    await session.flush()
    return version


async def persist_test_report(
    session: AsyncSession,
    root: Path,
    task_id: UUID,
    report: TestReport,
) -> int:
    """Store one runner report and return its version. The caller commits.

    The same bytes reuse the existing version. This function does not run pytest
    and does not write the registered checkout.
    """
    try:
        if not isinstance(root, Path):
            raise TestReportStoreError
        payload = test_report_document(report)
        digest = checksum(payload)
        existing = await session.scalar(
            select(Artifact).where(
                Artifact.task_id == task_id,
                Artifact.artifact_type == TEST_REPORT_ARTIFACT_TYPE,
                Artifact.checksum == digest,
            )
        )
        if existing is not None:
            return existing.version
        current = await session.scalar(
            select(func.max(Artifact.version)).where(
                Artifact.task_id == task_id,
                Artifact.artifact_type == TEST_REPORT_ARTIFACT_TYPE,
            )
        )
        version = int(current or 0) + 1
        if version > MAX_PLAN_VERSION:
            raise TestReportStoreError
        relative = test_report_relative_path(task_id, version)
        write_test_report_file(prepare_artifact_root(root), relative, payload)
        session.add(
            Artifact(
                id=uuid4(),
                task_id=task_id,
                artifact_type=TEST_REPORT_ARTIFACT_TYPE,
                version=version,
                relative_path=relative,
                checksum=digest,
                size=len(payload),
            )
        )
        await session.flush()
    except TestReportStoreError:
        raise
    except ArtifactStoreError:
        raise TestReportStoreError from None
    return version


async def persist_review(
    session: AsyncSession,
    root: Path,
    task_id: UUID,
    review: ReviewResult,
    *,
    policy: str,
    repair_count: int,
) -> int:
    """Store one review and return its version. The caller commits.

    The same bytes reuse the existing version. This function does not edit the
    worktree and does not run pytest.
    """
    if not isinstance(root, Path):
        raise ArtifactStoreError
    payload = review_document(review, policy=policy, repair_count=repair_count)
    digest = checksum(payload)
    existing = await session.scalar(
        select(Artifact).where(
            Artifact.task_id == task_id,
            Artifact.artifact_type == REVIEW_ARTIFACT_TYPE,
            Artifact.checksum == digest,
        )
    )
    if existing is not None:
        return existing.version
    current = await session.scalar(
        select(func.max(Artifact.version)).where(
            Artifact.task_id == task_id,
            Artifact.artifact_type == REVIEW_ARTIFACT_TYPE,
        )
    )
    version = int(current or 0) + 1
    if version > MAX_PLAN_VERSION:
        raise ArtifactStoreError
    relative = review_relative_path(task_id, version)
    write_review_file(prepare_artifact_root(root), relative, payload)
    session.add(
        Artifact(
            id=uuid4(),
            task_id=task_id,
            artifact_type=REVIEW_ARTIFACT_TYPE,
            version=version,
            relative_path=relative,
            checksum=digest,
            size=len(payload),
        )
    )
    await session.flush()
    return version
