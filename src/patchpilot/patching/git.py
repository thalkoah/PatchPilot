"""Git commands for applying one already-validated diff.

The argument list is fixed here. The model does not choose a command, a
remote, or a path outside the private worktree. There is no shell.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from patchpilot.patching.errors import PatchError

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 30
_ACTIONS = frozenset(
    {
        "apply-check",
        "apply",
        "add",
        "diff-cached",
        "reset-mixed",
        "reset-hard",
    }
)


def apply_check(worktree: Path, diff: str) -> None:
    """Refuse a diff that Git will not apply onto this worktree."""
    _git(worktree, "apply-check", _apply_args(worktree, check=True), diff)


def apply_diff(worktree: Path, diff: str) -> None:
    """Apply a diff that `apply_check` already accepted."""
    _git(worktree, "apply", _apply_args(worktree, check=False), diff)


def stage_paths(worktree: Path, paths: list[str]) -> None:
    """Stage only these relative paths. `.` and pathspecs from the model are not used."""
    _require_paths(paths)
    _git(
        worktree,
        "add",
        ["git", "-C", _path_arg(worktree), *_git_config(worktree), "add", "--", *paths],
        None,
    )


def cached_diff(worktree: Path, paths: list[str]) -> str:
    """Return the staged unified diff for these paths."""
    _require_paths(paths)
    return _git(
        worktree,
        "diff-cached",
        [
            "git",
            "-C",
            _path_arg(worktree),
            *_git_config(worktree),
            "diff",
            "--cached",
            "--no-ext-diff",
            "--",
            *paths,
        ],
        None,
    )


def unstage_paths(worktree: Path, paths: list[str]) -> None:
    """Unstage these paths and leave the worktree files in place."""
    _require_paths(paths)
    _git(
        worktree,
        "reset-mixed",
        ["git", "-C", _path_arg(worktree), *_git_config(worktree), "reset", "-q", "--", *paths],
        None,
    )


def reset_hard(worktree: Path) -> None:
    """Restore tracked files in this worktree to HEAD."""
    _git(
        worktree,
        "reset-hard",
        ["git", "-C", _path_arg(worktree), *_git_config(worktree), "reset", "--hard", "HEAD"],
        None,
    )


def _apply_args(worktree: Path, *, check: bool) -> list[str]:
    args = [
        "git",
        "-C",
        _path_arg(worktree),
        *_git_config(worktree),
        "apply",
        "--whitespace=nowarn",
    ]
    if check:
        args.append("--check")
    args.append("-")
    return args


def _require_paths(paths: list[str]) -> None:
    if not paths:
        raise PatchError("malformed")
    for path in paths:
        if not isinstance(path, str) or path.startswith("-") or "\n" in path or "\x00" in path:
            raise PatchError("malformed")


def _git(worktree: Path, action: str, args: list[str], stdin: str | None) -> str:
    if action not in _ACTIONS or not args or args[0] != "git":
        raise PatchError("apply_failed")
    if any(token in {"fetch", "push", "pull", "submodule", "clean"} for token in args):
        raise PatchError("apply_failed")
    git = shutil.which("git")
    if git is None:
        raise PatchError("apply_failed")
    command = [git, *args[1:]]
    try:
        with tempfile.TemporaryDirectory(prefix="patchpilot-patch-") as home:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                input=stdin,
                timeout=_GIT_TIMEOUT_SECONDS,
                cwd=_path_arg(worktree),
                env=_git_env(home),
            )
    except (OSError, subprocess.TimeoutExpired):
        logger.error("git %s failed", action)
        raise PatchError("apply_failed") from None
    if completed.returncode != 0:
        logger.error("git %s failed status=%s", action, completed.returncode)
        if action == "apply-check":
            raise PatchError("malformed")
        raise PatchError("apply_failed")
    return completed.stdout


def _git_config(worktree: Path) -> list[str]:
    return [
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.safecrlf=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "credential.helper=",
        "-c",
        f"safe.directory={_path_arg(worktree)}",
    ]


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
        raise PatchError("apply_failed")
    return text
