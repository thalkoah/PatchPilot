"""Stable failure codes for an isolated Git workspace.

Messages are the code itself. They do not include host paths or Git output.
"""


class WorkspaceError(Exception):
    """A workspace path or Git command was rejected."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkspaceStoreError(Exception):
    """The workspace row could not be stored. The caller still holds the lease."""
