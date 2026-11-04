"""Failures from the patch tool. The message is a code, not model text."""


class PatchError(Exception):
    """The patch was refused before it changed the worktree, or it was rolled back."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
