"""Hard ceilings for read-only repository tools.

Callers may pass a smaller `ToolLimits`. Values above these ceilings are rejected
so a setting cannot turn the limits off.
"""

from dataclasses import dataclass

HARD_MAX_TREE_ENTRIES = 2_000
HARD_MAX_TREE_DEPTH = 12
HARD_MAX_FILE_BYTES = 256 * 1024
HARD_MAX_FILE_LINES = 2_000
HARD_MAX_SEARCH_FILES = 1_000
HARD_MAX_SEARCH_MATCHES = 200
HARD_MAX_QUERY_LENGTH = 200
HARD_MAX_LINE_LENGTH = 400


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """Bounds for one tool instance. Defaults are the production values."""

    max_tree_entries: int = 200
    max_tree_depth: int = 6
    max_file_bytes: int = 32_768
    max_file_lines: int = 400
    max_search_files: int = 100
    max_search_matches: int = 40
    max_query_length: int = 200
    max_line_length: int = 200

    def __post_init__(self) -> None:
        checks = {
            "max_tree_entries": (self.max_tree_entries, HARD_MAX_TREE_ENTRIES),
            "max_tree_depth": (self.max_tree_depth, HARD_MAX_TREE_DEPTH),
            "max_file_bytes": (self.max_file_bytes, HARD_MAX_FILE_BYTES),
            "max_file_lines": (self.max_file_lines, HARD_MAX_FILE_LINES),
            "max_search_files": (self.max_search_files, HARD_MAX_SEARCH_FILES),
            "max_search_matches": (self.max_search_matches, HARD_MAX_SEARCH_MATCHES),
            "max_query_length": (self.max_query_length, HARD_MAX_QUERY_LENGTH),
            "max_line_length": (self.max_line_length, HARD_MAX_LINE_LENGTH),
        }
        for name, (value, ceiling) in checks.items():
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
                or value > ceiling
            ):
                raise ValueError(f"{name} must be from 1 to {ceiling}")


DEFAULT_LIMITS = ToolLimits()
