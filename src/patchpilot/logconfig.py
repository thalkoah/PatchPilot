"""Process logs stay short and do not keep credentials.

Docker log rotation bounds how much is stored. This filter bounds each line
and removes connection strings, bearer tokens, and `sk-` keys before a handler
writes them. It does not change the log level.
"""

import logging
import re

MAX_LOG_CHARS = 500

_URL_PASSWORD = re.compile(r"(?i)((?:postgres(?:ql)?|https?)://[^:\s/]+:)[^@\s/]+@")
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


class BoundedLogFilter(logging.Filter):
    """Truncate one record and replace credential-shaped text."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = _URL_PASSWORD.sub(r"\1[redacted]@", message)
        message = _BEARER.sub("Bearer [redacted]", message)
        message = _SK_KEY.sub("sk-[redacted]", message)
        if len(message) > MAX_LOG_CHARS:
            message = message[:MAX_LOG_CHARS] + "…"
        record.msg = message
        record.args = ()
        return True


def install_log_filter() -> None:
    """Attach the filter to handlers that are already installed."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, BoundedLogFilter) for item in handler.filters):
            handler.addFilter(BoundedLogFilter())


def configure_logging(level: str) -> None:
    """Install one stderr handler when the process has none, then filter every handler."""
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    install_log_filter()
