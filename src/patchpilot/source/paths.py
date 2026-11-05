"""Relative paths under one repository root.

A tool path is a slash-separated relative path. Absolute paths, `..`, dotfiles,
credential names, and `.git` never become a filesystem lookup.
"""

import re
import stat
from pathlib import Path

from patchpilot.source.errors import SourceError

# Matches CANONICAL_PATH_MAX_LENGTH on the repositories table.
_MAX_RELATIVE_LENGTH = 512

_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
_DENIED_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "secrets.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_DENIED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".kdbx"})
_ALLOWED_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".toml",
        ".md",
        ".txt",
        ".rst",
        ".ini",
        ".cfg",
        ".json",
        ".yaml",
        ".yml",
        ".in",
    }
)
_ALLOWED_BARE_NAMES = frozenset(
    {
        "readme",
        "dockerfile",
        "makefile",
        "license",
        "copying",
        "pipfile",
    }
)


def is_denied_name(name: str) -> bool:
    """True when this single path segment must not be listed or read."""
    lowered = name.casefold()
    if lowered in _DENIED_NAMES or lowered.startswith(".env"):
        return True
    if lowered.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")):
        return True
    suffix = Path(lowered).suffix
    return suffix in _DENIED_SUFFIXES


def is_allowed_text_name(name: str) -> bool:
    """True when a regular file name is on the text allowlist and not denied."""
    if is_denied_name(name):
        return False
    lowered = name.casefold()
    if lowered in _ALLOWED_BARE_NAMES:
        return True
    return Path(lowered).suffix in _ALLOWED_SUFFIXES


def relative_parts(value: str, *, allow_root: bool) -> tuple[str, ...]:
    """Split a tool path into safe segments. `.` is the checkout root."""
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise SourceError("invalid_path")
    raw = value.strip()
    while raw.startswith("./"):
        raw = raw[2:]
    if raw in {"", "."}:
        if allow_root:
            return ()
        raise SourceError("invalid_path")
    if raw.startswith(("/", "~")) or ":" in raw:
        raise SourceError("invalid_path")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", ".", ".."} or is_denied_name(part) or _SEGMENT.fullmatch(part) is None:
            raise SourceError("invalid_path")
        parts.append(part)
    canonical = "/".join(parts)
    if len(canonical) > _MAX_RELATIVE_LENGTH:
        raise SourceError("invalid_path")
    return tuple(parts)


def canonical_checkout_path(value: str) -> str:
    """Return the relative path stored for a registered checkout."""
    return "/".join(relative_parts(value, allow_root=False))


def is_hard_linked_file(path: Path) -> bool:
    """True when this regular file shares its inode with another directory entry.

    A hard link can name a file that also lives outside the checkout. Refusing
    it keeps reads and writes on the directory entry the tool was given.
    """
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink > 1


def is_inside(root: Path, candidate: Path) -> bool:
    """True when `candidate` is `root` or a path under it."""
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
