"""Task-scoped paths under the workspace volume.

The task id decides the branch and both relative paths. A model string is never
a path. The database checks the same three shapes.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from patchpilot.source.paths import is_inside
from patchpilot.workspace.errors import WorkspaceError

_SHA = re.compile(r"^[0-9a-f]{40}$")
_TASK_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True, slots=True)
class IsolatedWorkspace:
    """One private clone. Paths are relative to the workspace root."""

    task_id: UUID
    base_sha: str
    branch: str
    clone_path: str
    worktree_path: str

    def as_state(self) -> dict[str, str]:
        return {
            "base_sha": self.base_sha,
            "branch": self.branch,
            "clone_path": self.clone_path,
            "worktree_path": self.worktree_path,
        }


def branch_name(task_id: UUID) -> str:
    _require_task_id(task_id)
    return f"patchpilot/{task_id}"


def clone_relative(task_id: UUID) -> str:
    _require_task_id(task_id)
    return f"{task_id}/clone"


def worktree_relative(task_id: UUID) -> str:
    _require_task_id(task_id)
    return f"{task_id}/worktree"


def require_sha(value: str) -> str:
    """Return a full commit id. Abbreviations and uppercase are rejected."""
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise WorkspaceError("invalid_source")
    return value


def workspace_from_state(value: object, *, task_id: UUID) -> IsolatedWorkspace | None:
    """Accept only the layout derived from this task id.

    `None` means this task has no workspace. Any other path, including one a
    model wrote into the checkpoint, is rejected.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("workspace payload is not an object")
    sha = value.get("base_sha")
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        raise ValueError("workspace baseline is not a commit")
    if (
        value.get("branch") != branch_name(task_id)
        or value.get("clone_path") != clone_relative(task_id)
        or value.get("worktree_path") != worktree_relative(task_id)
    ):
        raise ValueError("workspace path was not assigned from the task id")
    return IsolatedWorkspace(
        task_id=task_id,
        base_sha=sha,
        branch=branch_name(task_id),
        clone_path=clone_relative(task_id),
        worktree_path=worktree_relative(task_id),
    )


def require_workspace_root(workspace_root: Path) -> Path:
    """Return the real workspace directory. It must already exist."""
    if not isinstance(workspace_root, Path) or not workspace_root.is_absolute():
        raise WorkspaceError("root_missing")
    try:
        root = workspace_root.resolve()
    except OSError:
        raise WorkspaceError("root_missing") from None
    if not root.is_dir():
        raise WorkspaceError("root_missing")
    return root


def require_source(source: Path, *, workspace_root: Path) -> Path:
    """Return the registered checkout. It must not overlap the workspace volume."""
    if not isinstance(source, Path):
        raise WorkspaceError("invalid_source")
    try:
        checkout = source.resolve()
        root = workspace_root.resolve()
    except OSError:
        raise WorkspaceError("invalid_source") from None
    if not checkout.is_dir() or not root.is_dir():
        raise WorkspaceError("invalid_source")
    git_dir = checkout / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise WorkspaceError("invalid_source")
    try:
        git_resolved = git_dir.resolve()
    except OSError:
        raise WorkspaceError("invalid_source") from None
    if not is_inside(checkout, git_resolved) or git_resolved == checkout:
        raise WorkspaceError("invalid_source")
    if is_inside(checkout, root) or is_inside(root, checkout):
        raise WorkspaceError("escapes_root")
    return checkout


def task_directory(workspace_root: Path, task_id: UUID) -> Path:
    """Return `<root>/<task id>` without following a symlink at that path."""
    root = require_workspace_root(workspace_root)
    _require_task_id(task_id)
    candidate = root / str(task_id)
    if candidate.is_symlink():
        raise WorkspaceError("escapes_root")
    if candidate.exists():
        try:
            resolved = candidate.resolve()
        except OSError:
            raise WorkspaceError("escapes_root") from None
        if resolved != candidate or not is_inside(root, resolved) or resolved == root:
            raise WorkspaceError("escapes_root")
    return candidate


def _require_task_id(task_id: UUID) -> None:
    if not isinstance(task_id, UUID) or _TASK_ID.fullmatch(str(task_id)) is None:
        raise WorkspaceError("invalid_source")
