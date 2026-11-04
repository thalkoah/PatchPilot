"""Private clones stay off the registered checkout."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from tests.database import open_session_factory

from patchpilot.repositories.repos import RepoRepository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.repositories.workspaces import insert_workspace
from patchpilot.workflow.graph import route_after_approval
from patchpilot.workspace.errors import WorkspaceError, WorkspaceStoreError
from patchpilot.workspace.git import prepare_isolated_workspace, remove_isolated_workspace
from patchpilot.workspace.layout import (
    IsolatedWorkspace,
    branch_name,
    clone_relative,
    workspace_from_state,
    worktree_relative,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo_service"


def test_approval_routes_an_accepted_plan_to_the_workspace():
    assert (
        route_after_approval({"status": "running", "approval": {"decision": "approve"}})
        == "prepare_workspace"
    )
    assert (
        route_after_approval({"status": "rejected", "approval": {"decision": "reject"}})
        == "finalize"
    )
    assert route_after_approval({"status": "failed", "error": "x"}) == "finalize"


def test_source_git_state_is_unchanged_after_create_and_teardown(tmp_path: Path):
    source = _git_repo(tmp_path / "src")
    before = git_snapshot(source)
    root = tmp_path / "workspaces"
    root.mkdir()
    task_id = uuid4()

    layout = prepare_isolated_workspace(source=source, workspace_root=root, task_id=task_id)

    assert layout.base_sha == _head(source)
    assert layout.branch == branch_name(task_id)
    assert layout.clone_path == clone_relative(task_id)
    assert layout.worktree_path == worktree_relative(task_id)
    assert git_snapshot(source) == before
    assert not (source / ".git" / "worktrees").exists()
    clone = root / layout.clone_path
    worktree = root / layout.worktree_path
    assert _head(worktree) == layout.base_sha
    assert _branch(worktree) == layout.branch
    assert _common_dir(worktree) == clone.resolve()
    assert _common_dir(worktree) != (source / ".git").resolve()
    assert _remotes(clone) == ""
    config = (clone / "config").read_text(encoding="utf-8")
    assert "https://" not in config
    assert "http://" not in config
    assert (worktree / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
    (worktree / "app.py").write_text("changed\n", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert git_snapshot(source) == before

    remove_isolated_workspace(root, task_id)

    assert git_snapshot(source) == before
    assert not (root / str(task_id)).exists()
    assert not (source / ".git" / "worktrees").exists()


def test_a_second_prepare_reuses_the_worktree(tmp_path: Path):
    source = _git_repo(tmp_path / "src")
    before = git_snapshot(source)
    root = tmp_path / "workspaces"
    root.mkdir()
    task_id = uuid4()
    first = prepare_isolated_workspace(source=source, workspace_root=root, task_id=task_id)
    worktree = root / first.worktree_path
    (worktree / "app.py").write_text("kept\n", encoding="utf-8")

    second = prepare_isolated_workspace(source=source, workspace_root=root, task_id=task_id)

    assert second == first
    assert (worktree / "app.py").read_text(encoding="utf-8") == "kept\n"
    assert git_snapshot(source) == before
    assert not (source / ".git" / "worktrees").exists()


def test_read_only_source_can_be_cloned(tmp_path: Path):
    source = _git_repo(tmp_path / "src")
    before = git_snapshot(source)
    modes = _modes(source)
    _deny_write(source)
    root = tmp_path / "workspaces"
    root.mkdir()
    try:
        layout = prepare_isolated_workspace(
            source=source,
            workspace_root=root,
            task_id=uuid4(),
        )
    finally:
        _restore_modes(modes)

    assert layout.base_sha == _head(source)
    assert git_snapshot(source) == before
    assert not (source / ".git" / "worktrees").exists()


def test_paths_come_from_the_task_id(tmp_path: Path):
    source = _git_repo(tmp_path / "src")
    (source / "notes.txt").write_text("../outside\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "--", "notes.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=PatchPilot Demo",
            "-c",
            "user.email=demo@example.invalid",
            "commit",
            "-m",
            "note",
        ],
        check=True,
    )
    root = tmp_path / "workspaces"
    root.mkdir()
    task_id = uuid4()

    layout = prepare_isolated_workspace(source=source, workspace_root=root, task_id=task_id)

    assert {path.name for path in root.iterdir()} == {str(task_id)}
    assert layout.worktree_path == f"{task_id}/worktree"
    assert not (root / "outside").exists()
    with pytest.raises(ValueError):
        workspace_from_state(
            {
                "base_sha": "a" * 40,
                "branch": branch_name(task_id),
                "clone_path": "../elsewhere/clone",
                "worktree_path": worktree_relative(task_id),
            },
            task_id=task_id,
        )


def test_a_symlink_task_directory_is_not_followed(tmp_path: Path):
    source = _git_repo(tmp_path / "src")
    before = git_snapshot(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("nope\n", encoding="utf-8")
    root = tmp_path / "workspaces"
    root.mkdir()
    task_id = uuid4()
    (root / str(task_id)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError) as caught:
        prepare_isolated_workspace(source=source, workspace_root=root, task_id=task_id)

    assert caught.value.code == "escapes_root"
    assert (outside / "secret").read_text(encoding="utf-8") == "nope\n"
    assert git_snapshot(source) == before


def test_a_symlink_git_directory_is_not_cloned(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    (source / ".git").symlink_to(git_dir, target_is_directory=True)
    root = tmp_path / "workspaces"
    root.mkdir()

    with pytest.raises(WorkspaceError) as caught:
        prepare_isolated_workspace(source=source, workspace_root=root, task_id=uuid4())

    assert caught.value.code == "invalid_source"
    assert list(root.iterdir()) == []
    assert list(git_dir.iterdir()) == []


def test_workspace_root_cannot_live_inside_the_source(tmp_path: Path):
    source = _git_repo(tmp_path / "src")
    nested = source / "workspaces"
    nested.mkdir()
    before = git_snapshot(source)

    with pytest.raises(WorkspaceError) as caught:
        prepare_isolated_workspace(source=source, workspace_root=nested, task_id=uuid4())

    assert caught.value.code == "escapes_root"
    assert git_snapshot(source) == before
    with pytest.raises(WorkspaceError):
        prepare_isolated_workspace(
            source=source,
            workspace_root=Path("workspaces"),
            task_id=uuid4(),
        )


def test_a_partial_directory_is_replaced(tmp_path: Path):
    source = _git_repo(tmp_path / "src")
    before = git_snapshot(source)
    root = tmp_path / "workspaces"
    root.mkdir()
    task_id = uuid4()
    junk = root / str(task_id)
    junk.mkdir()
    (junk / "junk.txt").write_text("partial\n", encoding="utf-8")

    layout = prepare_isolated_workspace(source=source, workspace_root=root, task_id=task_id)

    assert not (junk / "junk.txt").exists()
    assert (root / layout.worktree_path / "app.py").is_file()
    assert git_snapshot(source) == before


async def test_database_rejects_a_path_that_is_not_the_task_id(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            repository = await RepoRepository(session).save(
                display_name="Demo",
                canonical_path="demo_service",
                registration={"head": "0" * 40},
            )
            task = await TaskRepository(session).create(
                title="Save fails",
                description="The button fails.",
                repo_id=repository.id,
            )
            task_id = task.id
            repo_id = repository.id
        with psycopg.connect(postgres_database_url, autocommit=True) as connection:
            with pytest.raises(psycopg.Error):
                connection.execute(
                    """
                    INSERT INTO workspaces (
                        id, task_id, repo_id, base_sha, branch,
                        clone_path, worktree_path, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
                    """,
                    (
                        uuid4(),
                        task_id,
                        repo_id,
                        "a" * 40,
                        "patchpilot/not-the-task",
                        f"{task_id}/clone",
                        f"{task_id}/worktree",
                    ),
                )
        forged = IsolatedWorkspace(
            task_id=task_id,
            base_sha="b" * 40,
            branch="patchpilot/not-the-task",
            clone_path="../clone",
            worktree_path=worktree_relative(task_id),
        )
        async with factory() as session:
            with pytest.raises(WorkspaceStoreError):
                await insert_workspace(
                    session,
                    task_id=task_id,
                    repo_id=repo_id,
                    workspace=forged,
                )
    finally:
        await engine.dispose()


def copy_demo_repository(destination: Path) -> Path:
    """Copy the demo checkout and commit it. Return the repository root."""
    checkout = destination / "demo_service"
    shutil.copytree(
        DEMO,
        checkout,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    _commit_all(checkout)
    return destination


def git_snapshot(root: Path) -> dict[str, str]:
    """Content and kind of every path, including `.git`. Symlinks are not followed."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[relative] = "link:" + os.readlink(path)
        elif path.is_dir():
            found[relative] = "dir"
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[relative] = f"{path.stat().st_mode & 0o777:o}:{digest}"
    return found


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _commit_all(path)
    return path


def _commit_all(checkout: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-b", "main", str(checkout)], check=True)
    subprocess.run([git, "-C", str(checkout), "add", "--", "."], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(checkout),
            "-c",
            "user.name=PatchPilot Demo",
            "-c",
            "user.email=demo@example.invalid",
            "commit",
            "-m",
            "Baseline.",
        ],
        check=True,
    )


def _head(checkout: Path) -> str:
    return _git(checkout, "rev-parse", "HEAD")


def _branch(checkout: Path) -> str:
    return _git(checkout, "rev-parse", "--abbrev-ref", "HEAD")


def _common_dir(checkout: Path) -> Path:
    return Path(_git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def _remotes(clone: Path) -> str:
    return _git(clone, "remote")


def _git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _modes(root: Path) -> dict[Path, int]:
    return {path: path.stat().st_mode for path in [root, *root.rglob("*")] if not path.is_symlink()}


def _deny_write(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode & ~0o222)


def _restore_modes(modes: dict[Path, int]) -> None:
    for path, mode in modes.items():
        path.chmod(mode)
