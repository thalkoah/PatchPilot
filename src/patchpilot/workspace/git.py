"""Private clone and worktree for one approved task.

The registered checkout is read. Git metadata for the new worktree is written
only inside a bare clone under the workspace root. `git worktree add` is never
pointed at the original `.git`. There is no fetch and no push.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

from patchpilot.workspace.errors import WorkspaceError
from patchpilot.workspace.layout import (
    IsolatedWorkspace,
    branch_name,
    clone_relative,
    require_sha,
    require_source,
    require_workspace_root,
    task_directory,
    workspace_from_state,
    worktree_relative,
)

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 60
_ACTIONS = frozenset(
    {
        "rev-parse",
        "clone",
        "remote-remove",
        "remote-list",
        "worktree-add",
        "worktree-remove",
    }
)


def prepare_isolated_workspace(
    *,
    source: Path,
    workspace_root: Path,
    task_id: UUID,
) -> IsolatedWorkspace:
    """Clone `source` into this task's directory and check out its HEAD.

    A second call keeps the existing worktree when the branch and commit still
    match. Paths are derived from `task_id`. This function does not read a plan.
    """
    root = require_workspace_root(workspace_root)
    checkout = require_source(source, workspace_root=root)
    existing = _complete_workspace(root, task_id)
    if existing is not None:
        return existing
    directory = task_directory(root, task_id)
    if directory.exists():
        remove_isolated_workspace(root, task_id)
    directory.mkdir()
    try:
        sha = _source_head(checkout)
        clone = directory / "clone"
        worktree = directory / "worktree"
        _clone(checkout, clone)
        _remove_origin(clone)
        _add_worktree(clone, worktree, branch_name(task_id), sha)
        created = _complete_workspace(root, task_id)
        if created is None or created.base_sha != sha:
            raise WorkspaceError("workspace_failed")
        if (checkout / ".git" / "worktrees").exists():
            raise WorkspaceError("workspace_failed")
        return created
    except WorkspaceError:
        _discard(root, task_id)
        raise
    except Exception:
        logger.error("workspace failed: workspace_failed")
        _discard(root, task_id)
        raise WorkspaceError("workspace_failed") from None


def remove_isolated_workspace(workspace_root: Path, task_id: UUID) -> None:
    """Delete this task's directory. A missing directory is already clean.

    The source checkout is not an argument. Removal refuses a symlink where the
    task directory should be, so it cannot follow a link out of the volume.
    """
    root = require_workspace_root(workspace_root)
    directory = task_directory(root, task_id)
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise WorkspaceError("escapes_root")
    clone = directory / "clone"
    worktree = directory / "worktree"
    if (
        clone.is_dir()
        and not clone.is_symlink()
        and worktree.exists()
        and not worktree.is_symlink()
    ):
        try:
            _git(
                "worktree-remove",
                [
                    "git",
                    "-C",
                    _path_arg(clone),
                    *_git_config(clone),
                    "worktree",
                    "remove",
                    "--force",
                    _path_arg(worktree),
                ],
            )
        except WorkspaceError:
            logger.error("workspace cleanup could not detach the worktree")
    shutil.rmtree(directory)


def _complete_workspace(workspace_root: Path, task_id: UUID) -> IsolatedWorkspace | None:
    directory = task_directory(workspace_root, task_id)
    clone = directory / "clone"
    worktree = directory / "worktree"
    if not _real_dir(clone) or not _real_dir(worktree):
        return None
    try:
        sha = _rev_parse(worktree, "HEAD")
        branch = _rev_parse(worktree, "--abbrev-ref", "HEAD")
        bare = _rev_parse(clone, "--is-bare-repository")
        common = _rev_parse(worktree, "--path-format=absolute", "--git-common-dir")
        remotes = _remote_names(clone)
    except WorkspaceError:
        return None
    if bare != "true" or remotes:
        return None
    try:
        common_path = Path(common).resolve()
    except OSError:
        return None
    if common_path != clone.resolve():
        return None
    try:
        return workspace_from_state(
            {
                "base_sha": sha,
                "branch": branch,
                "clone_path": clone_relative(task_id),
                "worktree_path": worktree_relative(task_id),
            },
            task_id=task_id,
        )
    except ValueError:
        return None


def _source_head(checkout: Path) -> str:
    return require_sha(_rev_parse(checkout, "HEAD"))


def _clone(source: Path, destination: Path) -> None:
    _git(
        "clone",
        [
            "git",
            *_git_config(source),
            "clone",
            "--bare",
            "--no-hardlinks",
            "--no-recurse-submodules",
            "--",
            _path_arg(source),
            _path_arg(destination),
        ],
    )


def _remove_origin(clone: Path) -> None:
    if not _remote_names(clone):
        return
    _git(
        "remote-remove",
        ["git", "-C", _path_arg(clone), *_git_config(clone), "remote", "remove", "origin"],
    )
    if _remote_names(clone):
        raise WorkspaceError("workspace_failed")


def _add_worktree(clone: Path, worktree: Path, branch: str, sha: str) -> None:
    if branch.startswith("-") or sha.startswith("-"):
        raise WorkspaceError("workspace_failed")
    _git(
        "worktree-add",
        [
            "git",
            "-C",
            _path_arg(clone),
            *_git_config(clone, worktree),
            "worktree",
            "add",
            "-b",
            branch,
            _path_arg(worktree),
            sha,
        ],
    )


def _rev_parse(repository: Path, *args: str) -> str:
    if any(arg.startswith("-") and not arg.startswith("--") for arg in args):
        raise WorkspaceError("workspace_failed")
    return _git(
        "rev-parse",
        ["git", "-C", _path_arg(repository), *_git_config(repository), "rev-parse", *args],
    )


def _remote_names(clone: Path) -> list[str]:
    text = _git(
        "remote-list",
        ["git", "-C", _path_arg(clone), *_git_config(clone), "remote"],
    )
    return [line for line in text.splitlines() if line]


def _git(action: str, args: list[str]) -> str:
    if action not in _ACTIONS or not args or args[0] != "git":
        raise WorkspaceError("workspace_failed")
    if any(token in {"fetch", "push", "pull", "submodule"} for token in args):
        raise WorkspaceError("workspace_failed")
    git = shutil.which("git")
    if git is None:
        raise WorkspaceError("git_missing")
    command = [git, *args[1:]]
    try:
        with tempfile.TemporaryDirectory(prefix="patchpilot-git-") as home:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=_git_env(home),
            )
    except (OSError, subprocess.TimeoutExpired):
        logger.error("git %s failed", action)
        raise WorkspaceError("workspace_failed") from None
    if completed.returncode != 0:
        logger.error("git %s failed status=%s", action, completed.returncode)
        raise WorkspaceError("workspace_failed")
    return completed.stdout.strip()


def _git_config(*directories: Path) -> list[str]:
    config = [
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.allow=never",
        "-c",
        "credential.helper=",
    ]
    for directory in directories:
        config.extend(["-c", f"safe.directory={_path_arg(directory)}"])
    return config


def _git_env(home: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ALLOW_PROTOCOL": "file",
    }


def _path_arg(path: Path) -> str:
    text = str(path)
    if text.startswith("-") or "\n" in text or "\x00" in text:
        raise WorkspaceError("invalid_source")
    return text


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _discard(workspace_root: Path, task_id: UUID) -> None:
    try:
        remove_isolated_workspace(workspace_root, task_id)
    except WorkspaceError:
        logger.error("workspace cleanup failed")
