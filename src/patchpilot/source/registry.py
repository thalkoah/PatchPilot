"""Register a Git checkout that lives under `REPOSITORY_ROOT`.

The stored path is relative. Registration reads `.git/HEAD` only to record the
commit id. It does not copy remotes or absolute paths into PostgreSQL.
"""

import re
from pathlib import Path

from patchpilot.db.models import DISPLAY_NAME_MAX_LENGTH, Repository
from patchpilot.repositories.repos import RepoRepository
from patchpilot.source.errors import SourceError
from patchpilot.source.paths import canonical_checkout_path, is_inside

_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_REF = re.compile(r"^refs/[A-Za-z0-9._/-]+$")
_HEAD_LIMIT = 256


async def register_checkout(
    repository: RepoRepository,
    *,
    repository_root: Path,
    relative_path: str,
    display_name: str,
) -> Repository:
    """Insert or refresh one enabled checkout. The same path keeps its id."""
    name = _display_name(display_name)
    canonical = canonical_checkout_path(relative_path)
    checkout = _checkout_directory(repository_root, canonical)
    head = _read_head(checkout / ".git")
    return await repository.save(
        display_name=name,
        canonical_path=canonical,
        registration={"head": head},
    )


def _display_name(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise SourceError("invalid_name")
    name = value.strip()
    if name == "" or len(name) > DISPLAY_NAME_MAX_LENGTH:
        raise SourceError("invalid_name")
    return name


def _checkout_directory(repository_root: Path, canonical: str) -> Path:
    try:
        root = repository_root.resolve()
    except OSError:
        raise SourceError("root_missing") from None
    if not root.is_dir():
        raise SourceError("root_missing")
    lexical = root.joinpath(*canonical.split("/"))
    if lexical.is_symlink():
        try:
            checkout = lexical.resolve()
        except OSError:
            raise SourceError("escapes_root") from None
        if not is_inside(root, checkout) or checkout == root:
            raise SourceError("escapes_root")
    else:
        checkout = lexical
    if not checkout.is_dir():
        raise SourceError("not_a_checkout")
    git_dir = checkout / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise SourceError("not_a_checkout")
    if not is_inside(checkout, git_dir.resolve()):
        raise SourceError("not_a_checkout")
    return checkout


def _read_head(git_dir: Path) -> str:
    """Return the checked-out commit. Refuse a `.git` that points elsewhere."""
    head_path = git_dir / "HEAD"
    text = _read_small(head_path, git_dir)
    if _SHA.fullmatch(text):
        return text
    if not text.startswith("ref: "):
        raise SourceError("not_a_checkout")
    ref_name = text.removeprefix("ref: ").strip()
    if _REF.fullmatch(ref_name) is None or ".." in ref_name.split("/"):
        raise SourceError("not_a_checkout")
    ref_path = git_dir.joinpath(*ref_name.split("/"))
    sha = _read_small(ref_path, git_dir)
    if _SHA.fullmatch(sha) is None:
        raise SourceError("not_a_checkout")
    return sha


def _read_small(path: Path, git_dir: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SourceError("not_a_checkout")
    try:
        resolved = path.resolve()
    except OSError:
        raise SourceError("not_a_checkout") from None
    if not is_inside(git_dir.resolve(), resolved):
        raise SourceError("not_a_checkout")
    try:
        if path.lstat().st_size > _HEAD_LIMIT:
            raise SourceError("not_a_checkout")
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise SourceError("not_a_checkout") from None
    return text
