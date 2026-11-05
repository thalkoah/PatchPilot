"""One local model call at a time, in this process and across workers."""

import threading
from types import TracebackType

import psycopg

from patchpilot.llm.errors import LLMBudgetError

# Stable key for pg_try_advisory_lock. It is not a secret.
LOCAL_MODEL_LOCK_KEY = 71620251116

_MESSAGE = "local model parallelism is limited to one"


class LocalGate:
    """In-process slot. A second acquire fails instead of waiting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held = 0

    def acquire(self) -> None:
        with self._lock:
            if self._held >= 1:
                raise LLMBudgetError(_MESSAGE)
            self._held += 1

    def release(self) -> None:
        with self._lock:
            self._held = max(0, self._held - 1)


class LocalAdvisoryLock:
    """Session advisory lock. The connection stays open until release."""

    def __init__(self, database_url: str) -> None:
        self._held = False
        self._connection: psycopg.Connection | None = None
        try:
            self._connection = psycopg.connect(database_url, autocommit=True)
            row = self._connection.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (LOCAL_MODEL_LOCK_KEY,),
            ).fetchone()
        except psycopg.Error:
            self._close_quietly()
            raise LLMBudgetError(_MESSAGE) from None
        if row is None or row[0] is not True:
            self._close_quietly()
            raise LLMBudgetError(_MESSAGE)
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        connection = self._connection
        if connection is None:
            return
        try:
            connection.execute(
                "SELECT pg_advisory_unlock(%s)",
                (LOCAL_MODEL_LOCK_KEY,),
            )
        except psycopg.Error:
            pass
        self._close_quietly()
        self._held = False

    def _close_quietly(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is None:
            return
        try:
            connection.close()
        except psycopg.Error:
            pass
        self._connection = None

    def __enter__(self) -> "LocalAdvisoryLock":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
