"""The developer patch tool refuses anything outside the approved plan."""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from patchpilot.llm.patch import FileEdit, PatchProposal
from patchpilot.llm.plan import ChangePlan, ModificationKind, ProposedModification
from patchpilot.patching.apply import apply_patch
from patchpilot.patching.errors import PatchError
from patchpilot.workspace.git import prepare_isolated_workspace

SUMMARY = "Replace the constant and add a regression test."


def test_clean_edit_applies_inside_the_worktree_only(tmp_path: Path):
    source, worktree = _worktree(tmp_path)
    before = _snapshot(source)
    proposal = _edit_proposal(worktree, "src/app.py", "VALUE = 2\n")
    proposal = PatchProposal(
        summary=SUMMARY,
        edits=[
            *proposal.edits,
            FileEdit(
                relative_path="tests/test_added.py",
                expected_sha256="",
                content="def test_added():\n    assert True\n",
            ),
        ],
    )

    applied = apply_patch(worktree=worktree, proposal=proposal, plan=_plan())

    assert applied.summary == SUMMARY
    assert applied.paths == ("src/app.py", "tests/test_added.py")
    assert b"VALUE = 2" in (worktree / "src/app.py").read_bytes()
    added = (worktree / "tests/test_added.py").read_text(encoding="utf-8")
    assert added.startswith("def test_added")
    assert _snapshot(source) == before
    assert not (source / "tests/test_added.py").exists()
    text = applied.diff.decode("utf-8")
    assert "diff --git a/src/app.py b/src/app.py" in text
    assert "diff --git a/tests/test_added.py" in text
    assert proposal.model_dump_json() not in text


def test_path_traversal_does_not_leave_the_worktree(tmp_path: Path):
    source, worktree = _worktree(tmp_path)
    source_before = _snapshot(source)
    outside = tmp_path / "outside.txt"
    outside.write_text("safe\n", encoding="utf-8")
    before = outside.read_bytes()
    proposal = PatchProposal(
        summary="Escape the worktree.",
        unified_diff=(
            "diff --git a/../outside.txt b/../outside.txt\n"
            "--- a/../outside.txt\n"
            "+++ b/../outside.txt\n"
            "@@ -1 +1 @@\n"
            "-safe\n"
            "+owned\n"
        ),
    )

    with pytest.raises(PatchError) as caught:
        apply_patch(worktree=worktree, proposal=proposal, plan=_plan())

    assert caught.value.code == "denied_path"
    assert outside.read_bytes() == before
    assert (worktree / "src/app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _snapshot(source) == source_before


def test_binary_edit_is_refused(tmp_path: Path):
    _source, worktree = _worktree(tmp_path)
    original = (worktree / "src/app.py").read_bytes()
    proposal = PatchProposal(
        summary="Write a binary.",
        edits=[
            FileEdit(
                relative_path="src/app.py",
                expected_sha256=hashlib.sha256(original).hexdigest(),
                content="VALUE = 1\n\x00",
            )
        ],
    )

    with pytest.raises(PatchError) as caught:
        apply_patch(worktree=worktree, proposal=proposal, plan=_plan())

    assert caught.value.code == "binary"
    assert (worktree / "src/app.py").read_bytes() == original


def test_oversized_diff_is_refused(tmp_path: Path):
    _source, worktree = _worktree(tmp_path)
    original = (worktree / "src/app.py").read_bytes()
    proposal = PatchProposal(
        summary="Write far too much.",
        edits=[
            FileEdit(
                relative_path="src/app.py",
                expected_sha256=hashlib.sha256(original).hexdigest(),
                content="VALUE = 1\n" + ("x" * 17_000),
            )
        ],
    )

    with pytest.raises(PatchError) as caught:
        apply_patch(worktree=worktree, proposal=proposal, plan=_plan())

    assert caught.value.code == "oversized"
    assert (worktree / "src/app.py").read_bytes() == original


def test_a_path_outside_the_plan_is_refused(tmp_path: Path):
    _source, worktree = _worktree(tmp_path)
    original = (worktree / "src/other.py").read_bytes()
    proposal = _edit_proposal(worktree, "src/other.py", "VALUE = 9\n")

    with pytest.raises(PatchError) as caught:
        apply_patch(worktree=worktree, proposal=proposal, plan=_plan())

    assert caught.value.code == "outside_plan"
    assert (worktree / "src/other.py").read_bytes() == original


def test_a_malformed_diff_is_refused(tmp_path: Path):
    _source, worktree = _worktree(tmp_path)
    original = (worktree / "src/app.py").read_bytes()
    proposal = PatchProposal(summary="Not a diff.", unified_diff="this is not a unified diff\n")

    with pytest.raises(PatchError) as caught:
        apply_patch(worktree=worktree, proposal=proposal, plan=_plan())

    assert caught.value.code == "malformed"
    assert (worktree / "src/app.py").read_bytes() == original


def test_a_base_mismatch_is_refused(tmp_path: Path):
    _source, worktree = _worktree(tmp_path)
    original = (worktree / "src/app.py").read_bytes()
    proposal = PatchProposal(
        summary="Stale base.",
        edits=[
            FileEdit(
                relative_path="src/app.py",
                expected_sha256="0" * 64,
                content="VALUE = 2\n",
            )
        ],
    )

    with pytest.raises(PatchError) as caught:
        apply_patch(worktree=worktree, proposal=proposal, plan=_plan())

    assert caught.value.code == "base_mismatch"
    assert (worktree / "src/app.py").read_bytes() == original


def test_a_symlink_is_not_a_writable_target(tmp_path: Path):
    source, worktree = _worktree(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE = 1\n", encoding="utf-8")
    link = worktree / "src/link.py"
    link.symlink_to(outside)
    proposal = PatchProposal(
        summary="Write through a symlink.",
        edits=[
            FileEdit(
                relative_path="src/link.py",
                expected_sha256="",
                content="OUTSIDE = 2\n",
            )
        ],
    )
    plan = ChangePlan(
        problem_statement="The link should not be followed.",
        modifications=[
            ProposedModification(
                relative_path="src/link.py",
                kind=ModificationKind.EXISTING,
                summary="Replace the link.",
            )
        ],
        test_strategy="Do not run tests.",
        confidence=0.2,
        can_proceed=True,
    )

    with pytest.raises(PatchError) as caught:
        apply_patch(worktree=worktree, proposal=proposal, plan=plan)

    assert caught.value.code == "denied_path"
    assert outside.read_text(encoding="utf-8") == "OUTSIDE = 1\n"
    assert link.is_symlink()
    assert _snapshot(source) == _snapshot(source)


def _plan() -> ChangePlan:
    return ChangePlan(
        problem_statement="The constant is wrong.",
        modifications=[
            ProposedModification(
                relative_path="src/app.py",
                kind=ModificationKind.EXISTING,
                summary="Set the constant.",
            ),
            ProposedModification(
                relative_path="tests/test_added.py",
                kind=ModificationKind.NEW_TEST,
                summary="Cover the constant.",
            ),
        ],
        test_strategy="Run the unit test later.",
        confidence=0.5,
        can_proceed=True,
    )


def _edit_proposal(worktree: Path, relative: str, content: str) -> PatchProposal:
    payload = (worktree / relative).read_bytes()
    return PatchProposal(
        summary=SUMMARY,
        edits=[
            FileEdit(
                relative_path=relative,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                content=content,
            )
        ],
    )


def _worktree(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "src/other.py").write_text("VALUE = 0\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests/test_app.py").write_text(
        "def test_app():\n    assert True\n",
        encoding="utf-8",
    )
    _commit(source)
    root = tmp_path / "workspaces"
    root.mkdir()
    created = prepare_isolated_workspace(
        source=source,
        workspace_root=root,
        task_id=_task_id(tmp_path),
    )
    return source, root / created.worktree_path


def _task_id(tmp_path: Path):
    from uuid import UUID

    digest = hashlib.sha256(str(tmp_path).encode()).hexdigest()
    return UUID(digest[:32])


def _commit(checkout: Path) -> None:
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


def _snapshot(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or path.is_symlink() or not path.is_file():
            continue
        found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found
