"""Publication failures. The message is a code, never a token or a response body."""


class GitHubError(Exception):
    """A publication step stopped. `code` is safe to log."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
