"""Which proposed paths a later edit would be allowed to touch.

An existing change must already be a regular `.py` file in the checkout.
A new test must be `tests/**/test_*.py` under a directory that already exists.
Symlinks, traversal, secrets, and every other name are rejected. Nothing here writes.
"""

from pathlib import Path

from patchpilot.llm.plan import ModificationKind
from patchpilot.source.errors import SourceError
from patchpilot.source.paths import is_allowed_text_name, is_inside, relative_parts


def modification_is_allowed(root: Path, relative_path: str, kind: ModificationKind) -> bool:
    """True when this path may be named in a plan for this checkout."""
    if not isinstance(root, Path) or not isinstance(kind, ModificationKind):
        return False
    try:
        checkout = root.resolve()
        parts = relative_parts(relative_path, allow_root=False)
    except (OSError, SourceError):
        return False
    if not checkout.is_dir() or not parts:
        return False
    current = checkout
    for part in parts[:-1]:
        if not _real_directory(checkout, current / part):
            return False
        current = current / part
    return _target_is_allowed(checkout, current / parts[-1], parts, kind)


def _real_directory(root: Path, child: Path) -> bool:
    try:
        if child.is_symlink() or not child.is_dir():
            return False
        resolved = child.resolve()
    except OSError:
        return False
    return is_inside(root, resolved)


def _target_is_allowed(
    root: Path,
    child: Path,
    parts: tuple[str, ...],
    kind: ModificationKind,
) -> bool:
    name = parts[-1]
    if not name.endswith(".py") or not is_allowed_text_name(name):
        return False
    try:
        if child.is_symlink():
            return False
        exists = child.exists()
    except OSError:
        return False
    if exists:
        try:
            if not child.is_file():
                return False
            resolved = child.resolve()
        except OSError:
            return False
        if not is_inside(root, resolved):
            return False
    else:
        try:
            parent = child.parent.resolve()
        except OSError:
            return False
        if not is_inside(root, parent):
            return False
    if kind is ModificationKind.EXISTING:
        return exists
    if kind is ModificationKind.NEW_TEST:
        return _is_new_test(parts)
    return False


def _is_new_test(parts: tuple[str, ...]) -> bool:
    """A new or existing pytest module under a real `tests` directory."""
    if "tests" not in parts[:-1]:
        return False
    stem = parts[-1][: -len(".py")]
    return stem.startswith("test_") and len(stem) > len("test_")
