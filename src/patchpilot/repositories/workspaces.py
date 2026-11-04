"""One private workspace row per task. The caller commits an insert."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.db.models import Workspace, WorkspaceStatus
from patchpilot.workspace.errors import WorkspaceError, WorkspaceStoreError
from patchpilot.workspace.git import remove_isolated_workspace
from patchpilot.workspace.layout import IsolatedWorkspace, workspace_from_state


async def insert_workspace(
    session: AsyncSession,
    *,
    task_id: UUID,
    repo_id: UUID,
    workspace: IsolatedWorkspace,
) -> Workspace:
    """Insert the association, or return the matching active row.

    The relative paths are checked against the task id again. A stored path is
    never taken from the row and used as a delete target.
    """
    checked = _matching(task_id, workspace)
    existing = await session.scalar(select(Workspace).where(Workspace.task_id == task_id))
    if existing is not None:
        if _same_active(existing, checked):
            return existing
        raise WorkspaceStoreError
    row = Workspace(
        id=uuid4(),
        task_id=task_id,
        repo_id=repo_id,
        base_sha=checked.base_sha,
        branch=checked.branch,
        clone_path=checked.clone_path,
        worktree_path=checked.worktree_path,
        status=WorkspaceStatus.ACTIVE,
        removed_at=None,
    )
    session.add(row)
    await session.flush()
    return row


class WorkspaceRepository:
    """Reads the association and retires it. Retirement commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_task(self, task_id: UUID) -> Workspace | None:
        return await self._session.scalar(select(Workspace).where(Workspace.task_id == task_id))

    async def retire(self, task_id: UUID, workspace_root: Path) -> Workspace | None:
        """Delete the task directory and mark the row removed.

        A second call returns the removed row. The directory is the task id
        under `workspace_root`, not the path text stored on the row.
        """
        if not isinstance(workspace_root, Path):
            raise WorkspaceStoreError
        row = await self._session.scalar(
            select(Workspace).where(Workspace.task_id == task_id).with_for_update()
        )
        if row is None:
            await self._session.rollback()
            return None
        if row.status == WorkspaceStatus.REMOVED:
            stored = _detached(row)
            await self._session.rollback()
            return stored
        try:
            _matching(
                task_id,
                IsolatedWorkspace(
                    task_id=task_id,
                    base_sha=row.base_sha,
                    branch=row.branch,
                    clone_path=row.clone_path,
                    worktree_path=row.worktree_path,
                ),
            )
            remove_isolated_workspace(workspace_root, task_id)
        except (ValueError, WorkspaceError, WorkspaceStoreError):
            await self._session.rollback()
            raise WorkspaceStoreError from None
        row.status = WorkspaceStatus.REMOVED
        row.removed_at = datetime.now(UTC)
        await self._session.commit()
        return row


def _matching(task_id: UUID, workspace: IsolatedWorkspace) -> IsolatedWorkspace:
    try:
        checked = workspace_from_state(workspace.as_state(), task_id=task_id)
    except ValueError:
        raise WorkspaceStoreError from None
    if checked is None or checked != workspace or workspace.task_id != task_id:
        raise WorkspaceStoreError
    return checked


def _detached(row: Workspace) -> Workspace:
    """Copy loaded columns so a later rollback cannot expire them."""
    return Workspace(
        id=row.id,
        task_id=row.task_id,
        repo_id=row.repo_id,
        base_sha=row.base_sha,
        branch=row.branch,
        clone_path=row.clone_path,
        worktree_path=row.worktree_path,
        status=row.status,
        created_at=row.created_at,
        removed_at=row.removed_at,
    )


def _same_active(row: Workspace, workspace: IsolatedWorkspace) -> bool:
    return (
        row.status == WorkspaceStatus.ACTIVE
        and row.base_sha == workspace.base_sha
        and row.branch == workspace.branch
        and row.clone_path == workspace.clone_path
        and row.worktree_path == workspace.worktree_path
    )
