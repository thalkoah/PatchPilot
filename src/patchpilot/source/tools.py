"""Read-only inspection of one registered checkout.

`list_tree`, `read_text_file`, and `search_text` accept relative paths only.
They do not follow a symlink that leaves the checkout, and they do not read
`.git`, credential files, binaries, or files past the byte limit.
"""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.source.errors import SourceError
from patchpilot.source.limits import DEFAULT_LIMITS, ToolLimits
from patchpilot.source.paths import (
    canonical_checkout_path,
    is_allowed_text_name,
    is_denied_name,
    is_inside,
    relative_parts,
)


class TreeEntry(BaseModel):
    """One file, directory, or symlink under the checkout."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str = Field(pattern="^(file|dir|symlink)$")
    size: int | None = None


class TreeListing(BaseModel):
    """A bounded directory listing. Paths are relative to the checkout."""

    model_config = ConfigDict(extra="forbid")

    path: str
    entries: list[TreeEntry]
    truncated: bool


class TextFile(BaseModel):
    """UTF-8 text read from one allowlisted file."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    line_count: int
    byte_count: int
    truncated: bool


class SearchMatch(BaseModel):
    """One literal match. `line` is 1-based."""

    model_config = ConfigDict(extra="forbid")

    path: str
    line: int
    text: str


class SearchResult(BaseModel):
    """Matches from a bounded scan. The query is literal text, not a regex."""

    model_config = ConfigDict(extra="forbid")

    query: str
    matches: list[SearchMatch]
    files_scanned: int
    truncated: bool


class RepositoryTools:
    """File tools rooted at one resolved checkout directory."""

    def __init__(self, checkout: Path, *, limits: ToolLimits | None = None) -> None:
        root = checkout.resolve()
        if not root.is_dir():
            raise SourceError("not_found")
        self._root = root
        self._limits = limits if limits is not None else DEFAULT_LIMITS

    @property
    def root(self) -> Path:
        return self._root

    def list_tree(self, relative_path: str = ".") -> TreeListing:
        """List files and directories without following symlinks."""
        start = self._resolve(relative_path, allow_root=True, kind="dir")
        display = _display(self._root, start)
        entries: list[TreeEntry] = []
        truncated = False
        queue: list[tuple[Path, int]] = [(start, 0)]
        while queue:
            directory, depth = queue.pop(0)
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name)
            except OSError:
                raise SourceError("unreadable") from None
            for child in children:
                if is_denied_name(child.name) or _has_control_char(child.name):
                    continue
                try:
                    linked = child.is_symlink()
                except OSError:
                    raise SourceError("unreadable") from None
                rel = _display(self._root, child)
                if linked:
                    entries.append(TreeEntry(path=rel, kind="symlink"))
                elif child.is_dir():
                    entries.append(TreeEntry(path=rel, kind="dir"))
                    if depth + 1 < self._limits.max_tree_depth:
                        queue.append((child, depth + 1))
                    else:
                        truncated = True
                elif child.is_file() and is_allowed_text_name(child.name):
                    try:
                        size = child.lstat().st_size
                    except OSError:
                        raise SourceError("unreadable") from None
                    entries.append(TreeEntry(path=rel, kind="file", size=size))
                if len(entries) >= self._limits.max_tree_entries:
                    return TreeListing(path=display, entries=entries, truncated=True)
        return TreeListing(path=display, entries=entries, truncated=truncated)

    def read_text_file(self, relative_path: str) -> TextFile:
        """Read one allowlisted UTF-8 file, refusing binaries and oversized files."""
        path = self._resolve(relative_path, allow_root=False, kind="file")
        data = _read_capped(path, self._limits.max_file_bytes)
        if b"\x00" in data:
            raise SourceError("binary")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceError("binary") from None
        lines = text.splitlines()
        truncated = False
        if len(lines) > self._limits.max_file_lines:
            lines = lines[: self._limits.max_file_lines]
            truncated = True
            text = "\n".join(lines)
        return TextFile(
            path=_display(self._root, path),
            content=text,
            line_count=len(lines),
            byte_count=len(data),
            truncated=truncated,
        )

    def search_text(self, query: str, relative_path: str = ".") -> SearchResult:
        """Find a literal substring. Symlinks, secrets, and oversized files are skipped."""
        if not isinstance(query, str) or "\x00" in query or query == "":
            raise SourceError("invalid_query")
        if len(query) > self._limits.max_query_length:
            raise SourceError("invalid_query")
        start = self._resolve(relative_path, allow_root=True, kind="any")
        matches: list[SearchMatch] = []
        scanned = 0
        files, truncated = _search_files(
            start,
            self._root,
            self._limits.max_search_files,
        )
        for path in files:
            try:
                size = path.lstat().st_size
            except OSError:
                continue
            if size > self._limits.max_file_bytes:
                continue
            try:
                data = _read_capped(path, self._limits.max_file_bytes)
            except SourceError:
                continue
            if b"\x00" in data:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if query not in line:
                    continue
                limit = self._limits.max_line_length
                snippet = line if len(line) <= limit else line[:limit]
                matches.append(
                    SearchMatch(path=_display(self._root, path), line=number, text=snippet)
                )
                if len(matches) >= self._limits.max_search_matches:
                    truncated = True
                    return SearchResult(
                        query=query,
                        matches=matches,
                        files_scanned=scanned,
                        truncated=truncated,
                    )
        return SearchResult(
            query=query,
            matches=matches,
            files_scanned=scanned,
            truncated=truncated,
        )

    def _resolve(self, relative_path: str, *, allow_root: bool, kind: str) -> Path:
        parts = relative_parts(relative_path, allow_root=allow_root)
        current = self._root
        for part in parts:
            child = current / part
            try:
                linked = child.is_symlink()
            except OSError:
                raise SourceError("unreadable") from None
            if linked:
                try:
                    target = child.resolve()
                except OSError:
                    raise SourceError("escapes_root") from None
                if not target.exists() or not is_inside(self._root, target):
                    raise SourceError("escapes_root")
                if any(is_denied_name(piece) for piece in target.relative_to(self._root).parts):
                    raise SourceError("denied")
                current = target
                continue
            if not child.exists():
                raise SourceError("not_found")
            current = child
        if not is_inside(self._root, current):
            raise SourceError("escapes_root")
        if kind == "dir" and not current.is_dir():
            raise SourceError("not_a_directory")
        if kind == "file":
            if current.is_dir() or not current.is_file():
                raise SourceError("not_a_file")
            if not is_allowed_text_name(current.name):
                raise SourceError("denied")
        return current


def open_repository_tools(
    repository_root: Path,
    canonical_path: str,
    *,
    limits: ToolLimits | None = None,
) -> RepositoryTools:
    """Open tools for one checkout under `repository_root`.

    `canonical_path` is the relative path stored in `repositories`. An absolute
    path or a path outside the root is rejected.
    """
    if not isinstance(repository_root, Path):
        raise SourceError("invalid_path")
    try:
        root = repository_root.resolve()
    except OSError:
        raise SourceError("root_missing") from None
    if not root.is_dir():
        raise SourceError("root_missing")
    relative = canonical_checkout_path(canonical_path)
    lexical = root.joinpath(*relative.split("/"))
    checkout = lexical.resolve() if lexical.is_symlink() else lexical
    if not is_inside(root, checkout) or checkout == root:
        raise SourceError("escapes_root")
    if not checkout.is_dir():
        raise SourceError("not_found")
    return RepositoryTools(checkout, limits=limits)


def _display(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise SourceError("escapes_root") from None
    if not relative.parts:
        return "."
    return relative.as_posix()


def _has_control_char(name: str) -> bool:
    return any(ord(character) < 32 for character in name)


def _read_capped(path: Path, max_bytes: int) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise SourceError("not_a_file")
        size = path.lstat().st_size
    except OSError:
        raise SourceError("unreadable") from None
    if size > max_bytes:
        raise SourceError("too_large")
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        raise SourceError("unreadable") from None
    if len(data) > max_bytes:
        raise SourceError("too_large")
    return data


def _search_files(start: Path, root: Path, limit: int) -> tuple[list[Path], bool]:
    if start.is_symlink():
        return [], False
    if start.is_file():
        allowed = is_allowed_text_name(start.name) and is_inside(root, start.resolve())
        return ([start] if allowed else []), False
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(start, followlinks=False):
        current = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not is_denied_name(name)
            and not _has_control_char(name)
            and not (current / name).is_symlink()
        )
        for name in sorted(filenames):
            if not is_allowed_text_name(name) or _has_control_char(name):
                continue
            candidate = current / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if not is_inside(root, candidate.resolve()):
                continue
            found.append(candidate)
            if len(found) > limit:
                return found[:limit], True
    return found, False
