"""Isolated Git workspaces. The registered checkout stays read-only."""

from patchpilot.workspace.errors import WorkspaceError, WorkspaceStoreError
from patchpilot.workspace.git import prepare_isolated_workspace, remove_isolated_workspace
from patchpilot.workspace.layout import (
    IsolatedWorkspace,
    branch_name,
    clone_relative,
    workspace_from_state,
    worktree_relative,
)

__all__ = [
    "IsolatedWorkspace",
    "WorkspaceError",
    "WorkspaceStoreError",
    "branch_name",
    "clone_relative",
    "prepare_isolated_workspace",
    "remove_isolated_workspace",
    "worktree_relative",
    "workspace_from_state",
]
