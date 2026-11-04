"""Copy a private worktree into the runner volume.

The worker does this. The copy is a tree of regular files under a directory
named by the task id. Symlinks, `.git`, and binary files are refused. The
request file is written only after the copy finishes, and it does not name
the source path.
"""

import os
import shutil
from pathlib import Path
from uuid import UUID

from patchpilot.runner.errors import RunnerRejected
from patchpilot.runner.limits import MAX_COPY_BYTES, MAX_COPY_FILES, MAX_FILE_BYTES
from patchpilot.runner.protocol import (
    prepare_runner_root,
    project_dir,
    read_report,
    request_path,
    write_request,
)
from patchpilot.source.paths import is_inside

_SKIP = frozenset({".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"})


def stage_project(worktree: Path, root: Path, job_id: UUID) -> None:
    """Publish one project. A result or request that is already there is kept."""
    try:
        base = prepare_runner_root(root, create=True)
        source = _real_directory(worktree)
        if is_inside(source, base) or is_inside(base, source):
            raise RunnerRejected
        if read_report(base, job_id) is not None:
            return
        published = request_path(base, job_id)
        if published.exists():
            if published.is_symlink() or not published.is_file():
                raise RunnerRejected
            return
        destination = project_dir(base, job_id)
        if destination.exists() or destination.is_symlink():
            _remove(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            _copy(source, destination)
        except Exception:
            _remove(destination)
            raise
        write_request(base, job_id)
    except RunnerRejected:
        raise
    except (OSError, ValueError):
        raise RunnerRejected from None


def remove_project(root: Path, job_id: UUID) -> None:
    """Delete the copied tree after the result is in memory. The result file stays."""
    try:
        destination = project_dir(prepare_runner_root(root, create=False), job_id)
    except (OSError, ValueError):
        raise RunnerRejected from None
    _remove(destination)


def _copy(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            raise RunnerRejected
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP)
        for name in dirnames:
            if (current / name).is_symlink():
                raise RunnerRejected
        relative = current.relative_to(source)
        target_dir = destination.joinpath(relative)
        if not is_inside(destination, target_dir):
            raise RunnerRejected
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(filenames):
            if name in _SKIP:
                continue
            file_path = current / name
            if file_path.is_symlink() or not file_path.is_file():
                raise RunnerRejected
            try:
                size = file_path.stat().st_size
            except OSError:
                raise RunnerRejected from None
            if size > MAX_FILE_BYTES:
                raise RunnerRejected
            total += size
            files += 1
            if files > MAX_COPY_FILES or total > MAX_COPY_BYTES:
                raise RunnerRejected
            try:
                payload = file_path.read_bytes()
            except OSError:
                raise RunnerRejected from None
            if b"\x00" in payload or len(payload) != size:
                raise RunnerRejected
            target = target_dir / name
            if not is_inside(destination, target):
                raise RunnerRejected
            try:
                target.write_bytes(payload)
            except OSError:
                raise RunnerRejected from None
    if files == 0:
        raise RunnerRejected


def _real_directory(path: Path) -> Path:
    if not isinstance(path, Path) or path.is_symlink() or not path.is_dir():
        raise RunnerRejected
    try:
        resolved = path.resolve()
    except OSError:
        raise RunnerRejected from None
    if resolved.is_symlink() or not resolved.is_dir():
        raise RunnerRejected
    return resolved


def _remove(path: Path) -> None:
    if path.is_symlink():
        raise RunnerRejected
    if not path.exists():
        return
    if not path.is_dir():
        raise RunnerRejected
    try:
        shutil.rmtree(path)
    except OSError:
        raise RunnerRejected from None
