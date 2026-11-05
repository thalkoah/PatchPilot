"""Bounded pause between retries of a transient model error."""

from collections.abc import Callable

MAX_BACKOFF_SECONDS = 8.0


def retry_pause(
    attempt: int,
    base_seconds: float,
    sleeper: Callable[[float], None],
) -> None:
    """Sleep after a failed attempt. A base of zero does not sleep."""
    if base_seconds <= 0 or attempt < 1:
        return
    delay = min(base_seconds * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
    if delay > 0:
        sleeper(delay)
