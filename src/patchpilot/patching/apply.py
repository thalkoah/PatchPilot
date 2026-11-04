"""Apply one validated patch inside a private worktree.

The tool checks the approved plan, canonical paths, and the current file bytes.
It runs `git apply --check` before `git apply`. It does not start a shell, and
it does not run the project tests. A refusal leaves the worktree as it was.
"""

import difflib
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from patchpilot.llm.patch import FileEdit, PatchProposal
from patchpilot.llm.plan import ChangePlan, ModificationKind
from patchpilot.patching.errors import PatchError
from patchpilot.patching.git import (
    apply_check,
    apply_diff,
    cached_diff,
    reset_hard,
    stage_paths,
    unstage_paths,
)
from patchpilot.patching.policy import approved_modification, path_is_writable
from patchpilot.source.errors import SourceError
from patchpilot.source.paths import is_inside, relative_parts

logger = logging.getLogger(__name__)

MAX_DIFF_BYTES = 16_384
_MAX_FILE_BYTES = 20_000


@dataclass(frozen=True, slots=True)
class AppliedPatch:
    """The diff Git applied. This is not the model proposal."""

    summary: str
    paths: tuple[str, ...]
    diff: bytes


@dataclass(frozen=True, slots=True)
class _File:
    """One approved path. `after` is the structured replacement, when the model sent one."""

    path: str
    before: bytes | None
    after: bytes | None
    unified: str | None = None


def apply_patch(*, worktree: Path, proposal: PatchProposal, plan: ChangePlan) -> AppliedPatch:
    """Apply `proposal` under `worktree` and return the diff Git recorded."""
    if not isinstance(proposal, PatchProposal) or not isinstance(plan, ChangePlan):
        raise PatchError("malformed")
    root = _require_worktree(worktree)
    files = [item for item in _intended_files(root, proposal, plan) if _is_change(item)]
    if not files:
        raise PatchError("empty")
    diff = _diff_to_apply(files, proposal)
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        raise PatchError("oversized")
    paths = [item.path for item in files]
    baseline = _snapshot(root)
    applied = False
    try:
        apply_check(root, diff)
        apply_diff(root, diff)
        applied = True
        _confirm(root, files, baseline)
        recorded = _record(root, paths)
    except PatchError:
        if applied:
            _rollback(root, files)
        raise
    except Exception:
        logger.error("patch apply failed")
        if applied:
            _rollback(root, files)
        raise PatchError("apply_failed") from None
    return AppliedPatch(summary=proposal.summary, paths=tuple(paths), diff=recorded)


def _require_worktree(worktree: Path) -> Path:
    if not isinstance(worktree, Path):
        raise PatchError("apply_failed")
    try:
        root = worktree.resolve()
    except OSError:
        raise PatchError("apply_failed") from None
    git_dir = root / ".git"
    if worktree.is_symlink() or not root.is_dir() or git_dir.is_symlink() or not git_dir.exists():
        raise PatchError("apply_failed")
    return root


def _intended_files(root: Path, proposal: PatchProposal, plan: ChangePlan) -> list[_File]:
    if proposal.edits:
        files = [_from_edit(root, edit, plan) for edit in proposal.edits]
    else:
        files = _from_unified_diff(root, proposal.unified_diff, plan)
    if not files or len(files) > 8 or len({item.path for item in files}) != len(files):
        raise PatchError("malformed")
    return files


def _from_edit(root: Path, edit: FileEdit, plan: ChangePlan) -> _File:
    path = _writable_path(root, edit.relative_path, plan)
    before = _read_current(root, path)
    after = _text_bytes(edit.content)
    if before != after:
        _check_base(before, edit.expected_sha256)
    return _File(path=path, before=before, after=after)


def _from_unified_diff(root: Path, diff: str, plan: ChangePlan) -> list[_File]:
    text = _checked_diff_text(diff)
    sections = _split_sections(text)
    if not sections:
        raise PatchError("malformed")
    files: list[_File] = []
    pieces: list[str] = []
    for header, body in sections:
        path = _header_path(header)
        _reject_dangerous_body(body, path)
        writable = _writable_path(root, path, plan)
        before = _read_current(root, writable)
        if _declares_new_file(body) and before is not None:
            raise PatchError("base_mismatch")
        if not _declares_new_file(body) and before is None:
            raise PatchError("base_mismatch")
        files.append(_File(path=writable, before=before, after=None))
        pieces.append(header + body)
    joined = "".join(pieces)
    if not joined.endswith("\n"):
        joined += "\n"
    files[0] = _File(path=files[0].path, before=files[0].before, after=None, unified=joined)
    return files


def _is_change(item: _File) -> bool:
    return item.after is None or item.before != item.after


def _diff_to_apply(files: list[_File], proposal: PatchProposal) -> str:
    if proposal.unified_diff.strip():
        text = next((item.unified for item in files if item.unified), None)
        if not text:
            raise PatchError("malformed")
        return text
    return _canonical_diff(files)


def _writable_path(root: Path, relative_path: str, plan: ChangePlan) -> str:
    modification = approved_modification(plan, relative_path)
    if not path_is_writable(root, modification):
        raise PatchError("denied_path")
    try:
        parts = relative_parts(modification.relative_path, allow_root=False)
    except SourceError:
        raise PatchError("denied_path") from None
    candidate = root.joinpath(*parts)
    cursor = candidate.parent
    while cursor != root:
        if cursor.is_symlink():
            raise PatchError("denied_path")
        cursor = cursor.parent
    if candidate.is_symlink():
        raise PatchError("denied_path")
    try:
        if candidate.exists() and not is_inside(root, candidate.resolve()):
            raise PatchError("denied_path")
    except OSError:
        raise PatchError("denied_path") from None
    if modification.kind is ModificationKind.EXISTING and not candidate.is_file():
        raise PatchError("denied_path")
    return "/".join(parts)


def _read_current(root: Path, relative_path: str) -> bytes | None:
    candidate = root.joinpath(*relative_path.split("/"))
    if candidate.is_symlink():
        raise PatchError("denied_path")
    if not candidate.exists():
        return None
    try:
        if not candidate.is_file():
            raise PatchError("denied_path")
        payload = candidate.read_bytes()
    except OSError:
        raise PatchError("denied_path") from None
    if b"\x00" in payload:
        raise PatchError("binary")
    if len(payload) > _MAX_FILE_BYTES:
        raise PatchError("oversized")
    return payload


def _check_base(before: bytes | None, expected: str) -> None:
    if before is None:
        if expected != "":
            raise PatchError("base_mismatch")
        return
    if expected == "" or hashlib.sha256(before).hexdigest() != expected:
        raise PatchError("base_mismatch")


def _text_bytes(content: str) -> bytes:
    if "\x00" in content:
        raise PatchError("binary")
    if "\r" in content:
        raise PatchError("malformed")
    if not content.endswith("\n"):
        content = f"{content}\n"
    payload = content.encode("utf-8")
    if len(payload) > _MAX_FILE_BYTES:
        raise PatchError("oversized")
    return payload


def _canonical_diff(files: list[_File]) -> str:
    chunks: list[str] = []
    for item in files:
        if item.after is None or item.before == item.after:
            raise PatchError("malformed")
        rendered = _render_file(item.path, item.before, item.after)
        if not rendered.strip():
            raise PatchError("empty")
        chunks.append(rendered)
    diff = "".join(chunks)
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        raise PatchError("oversized")
    return diff


def _render_file(path: str, before: bytes | None, after: bytes) -> str:
    old = None if before is None else before.decode("utf-8")
    body = "".join(
        difflib.unified_diff(
            [] if old is None else old.splitlines(keepends=True),
            after.decode("utf-8").splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if body and not body.endswith("\n"):
        body += "\n"
    header = f"diff --git a/{path} b/{path}\n"
    if old is None:
        header += "new file mode 100644\n"
    return header + body


def _confirm(root: Path, files: list[_File], baseline: dict[str, str]) -> None:
    allowed = {item.path for item in files}
    current = _snapshot(root)
    for item in files:
        payload = _read_current(root, item.path)
        if payload is None:
            raise PatchError("apply_failed")
        if item.after is not None and payload != item.after:
            raise PatchError("apply_failed")
        if item.after is None and payload == item.before:
            raise PatchError("empty")
    for path, digest in baseline.items():
        if path in allowed:
            continue
        if current.get(path) != digest:
            raise PatchError("outside_plan")
    for path in current:
        if path not in baseline and path not in allowed:
            raise PatchError("outside_plan")


def _snapshot(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            raise PatchError("denied_path")
        if not path.is_file():
            continue
        found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def _record(root: Path, paths: list[str]) -> bytes:
    stage_paths(root, paths)
    try:
        text = cached_diff(root, paths)
    finally:
        unstage_paths(root, paths)
    if not text.endswith("\n"):
        text += "\n"
    payload = text.encode("utf-8")
    if len(payload) > MAX_DIFF_BYTES:
        raise PatchError("oversized")
    if not text.startswith("diff --git "):
        raise PatchError("empty")
    recorded = _diff_paths(text)
    if set(recorded) != set(paths) or len(recorded) != len(paths):
        raise PatchError("apply_failed")
    return payload


def _rollback(root: Path, files: list[_File]) -> None:
    try:
        reset_hard(root)
    except PatchError:
        logger.error("patch rollback failed")
        return
    for item in files:
        if item.before is not None:
            continue
        candidate = root.joinpath(*item.path.split("/"))
        try:
            if candidate.is_symlink():
                if is_inside(root, candidate.parent.resolve()):
                    candidate.unlink()
                continue
            if candidate.is_file() and is_inside(root, candidate.resolve()):
                candidate.unlink()
        except OSError:
            logger.error("patch rollback failed")


def _checked_diff_text(diff: str) -> str:
    if "\x00" in diff or not diff.strip():
        raise PatchError("binary" if "\x00" in diff else "malformed")
    if "GIT binary patch" in diff or "\nBinary files " in f"\n{diff}":
        raise PatchError("binary")
    if diff.endswith("\n"):
        return diff
    return f"{diff}\n"


def _split_sections(diff: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    header: str | None = None
    body: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if header is not None:
                sections.append((header, "".join(body)))
            header = line if line.endswith("\n") else f"{line}\n"
            body = []
            continue
        if header is None:
            raise PatchError("malformed")
        body.append(line)
    if header is None:
        raise PatchError("malformed")
    sections.append((header, "".join(body)))
    return sections


def _header_path(header: str) -> str:
    line = header.strip()
    marker = "diff --git a/"
    if not line.startswith(marker) or " b/" not in line:
        raise PatchError("malformed")
    left, right = line[len(marker) :].split(" b/", 1)
    if left != right or not left:
        raise PatchError("malformed")
    return left


def _reject_dangerous_body(body: str, path: str) -> None:
    minus = 0
    plus = 0
    for line in body.splitlines():
        if line.startswith(("rename ", "copy ", "deleted file mode", "old mode", "new mode")):
            raise PatchError("malformed")
        if line.startswith("new file mode ") and line != "new file mode 100644":
            raise PatchError("denied_path")
        if line.startswith("--- "):
            minus += 1
            if line not in {f"--- a/{path}", "--- /dev/null"}:
                raise PatchError("denied_path")
        if line.startswith("+++ "):
            plus += 1
            if line != f"+++ b/{path}":
                raise PatchError("denied_path")
    if minus != 1 or plus != 1:
        raise PatchError("malformed")


def _declares_new_file(body: str) -> bool:
    return "--- /dev/null" in body.splitlines()


def _diff_paths(diff: str) -> list[str]:
    return [_header_path(header) for header, _body in _split_sections(diff)]
