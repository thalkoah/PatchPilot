"""Push one task branch. The default branch is not a destination.

The token is passed to Git through an askpass script and an environment
variable. It is not written into the remote URL or the repository config.
"""

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from patchpilot.github.errors import GitHubError
from patchpilot.github.names import GitHubRepo
from patchpilot.workspace.layout import branch_name

_TIMEOUT_SECONDS = 60


def push_task_branch(
    worktree: Path,
    *,
    task_id: UUID,
    repo: GitHubRepo,
    token: SecretStr | None,
    local_remote: Path | None = None,
) -> str:
    """Push `patchpilot/<task id>` and return the commit that was sent.

    `local_remote` is a bare repository path used by tests. Production leaves
    it empty and pushes to `https://github.com/<owner>/<name>.git`.
    """
    branch = branch_name(task_id)
    if branch in {"main", "master"} or branch.startswith("-"):
        raise GitHubError("branch_refused")
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    if refspec.startswith("refs/heads/main") or refspec.startswith("refs/heads/master"):
        raise GitHubError("branch_refused")
    remote, local = _remote(repo, local_remote, token)
    if not worktree.is_dir() or worktree.is_symlink():
        raise GitHubError("branch_refused")
    if _rev(worktree, "--abbrev-ref", "HEAD") != branch:
        raise GitHubError("branch_refused")
    head = _rev(worktree, "HEAD")
    _run_push(worktree, remote=remote, refspec=refspec, token=token, local=local)
    if _rev(worktree, "HEAD") != head:
        raise GitHubError("branch_refused")
    if token is not None:
        secret = token.get_secret_value()
        if secret and _config_contains(worktree, secret):
            raise GitHubError("rejected")
    return head


def _remote(
    repo: GitHubRepo,
    local_remote: Path | None,
    token: SecretStr | None,
) -> tuple[str, bool]:
    expected = repo.git_remote()
    if local_remote is None:
        if token is None:
            raise GitHubError("token_missing")
        if "@" in expected or "token" in expected.casefold():
            raise GitHubError("branch_refused")
        return expected, False
    if not isinstance(local_remote, Path) or not local_remote.is_absolute():
        raise GitHubError("branch_refused")
    if local_remote.is_symlink() or not local_remote.is_dir():
        raise GitHubError("branch_refused")
    text = str(local_remote)
    if "\n" in text or "\x00" in text or text.startswith("-"):
        raise GitHubError("branch_refused")
    return text, True


def _run_push(
    worktree: Path,
    *,
    remote: str,
    refspec: str,
    token: SecretStr | None,
    local: bool,
) -> None:
    git = shutil.which("git")
    if git is None:
        raise GitHubError("rejected")
    command = [
        git,
        "-C",
        str(worktree),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        f"safe.directory={worktree}",
    ]
    if local:
        command.extend(
            [
                "-c",
                f"safe.directory={remote}",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.file.allow=always",
            ]
        )
    else:
        command.extend(["-c", "protocol.allow=https"])
    command.extend(["push", remote, refspec])
    if any(part in {"pull", "fetch", "merge"} for part in command):
        raise GitHubError("branch_refused")
    try:
        with tempfile.TemporaryDirectory(prefix="patchpilot-push-") as home:
            env = _env(home, token=None if local else token)
            if not local:
                askpass = Path(home) / "askpass"
                _write_askpass(askpass)
                env["GIT_ASKPASS"] = str(askpass)
                env["GIT_ALLOW_PROTOCOL"] = "https"
            else:
                env["GIT_ALLOW_PROTOCOL"] = "file"
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                env=env,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubError("network") from None
    if completed.returncode != 0:
        raise GitHubError("network")


def _write_askpass(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "*Username*) printf '%s\\n' 'x-access-token' ;;\n"
        "*) printf '%s\\n' \"$PATCHPILOT_GIT_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _env(home: str, *, token: SecretStr | None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if token is not None:
        env["PATCHPILOT_GIT_TOKEN"] = token.get_secret_value()
    return env


def _rev(repository: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None or any(arg.startswith("-") and not arg.startswith("--") for arg in args):
        raise GitHubError("branch_refused")
    try:
        completed = subprocess.run(
            [git, "-C", str(repository), "rev-parse", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubError("branch_refused") from None
    if completed.returncode != 0:
        raise GitHubError("branch_refused")
    text = completed.stdout.strip()
    if args == ("HEAD",) and len(text) != 40:
        raise GitHubError("branch_refused")
    return text


def _config_contains(repository: Path, secret: str) -> bool:
    git = shutil.which("git")
    if git is None:
        return True
    try:
        completed = subprocess.run(
            [git, "-C", str(repository), "config", "--list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return secret in completed.stdout or secret in completed.stderr
