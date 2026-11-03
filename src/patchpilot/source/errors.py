"""Stable failure codes for repository registration and read-only tools.

Messages are the code itself. They do not include host paths or file contents.
"""


class SourceError(Exception):
    """A path, checkout, or tool limit was rejected."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
