"""Lease renewal on a thread the model call cannot block.

``generate_structured`` is synchronous. While it runs, the worker event loop
does not wake asyncio timers. This thread uses its own connection and extends
the lease only when the UPDATE still matches the open run.
"""

import logging
import threading
from uuid import UUID

import psycopg

from patchpilot.audit import require_lease, require_worker_id

logger = logging.getLogger(__name__)

_RENEW = """
UPDATE task_runs
SET lease_until = now() + make_interval(secs => %s)
WHERE id = %s
  AND worker_id = %s
  AND ended_at IS NULL
  AND lease_until > now()
"""


def renewal_interval(lease_seconds: float) -> float:
    """How often the owner extends the lease. Always shorter than the lease itself."""
    lease = require_lease(lease_seconds)
    return min(max(lease / 3, 0.05), lease)


class LeaseKeeper:
    """Renew one run until ``stop``. ``lost`` is true when a renewal matched nothing."""

    def __init__(
        self,
        database_url: str,
        *,
        run_id: UUID,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        self._database_url = database_url
        self._run_id = run_id
        self._worker_id = require_worker_id(worker_id)
        self._lease_seconds = require_lease(lease_seconds)
        self._interval = renewal_interval(self._lease_seconds)
        self._stop = threading.Event()
        self._lost_flag = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            name="patchpilot-lease",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def lost(self) -> bool:
        return self._lost_flag.is_set()

    def stop(self) -> bool:
        """Stop renewing. Return False when the lease was lost."""
        self._stop.set()
        self._thread.join(timeout=5)
        return not self._lost_flag.is_set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            outcome = self._renew_once()
            if outcome is False:
                self._lost_flag.set()
                return

    def _renew_once(self) -> bool | None:
        """True when extended, False when the lease is gone, None on a transient error."""
        try:
            with psycopg.connect(
                self._database_url,
                autocommit=True,
                connect_timeout=3,
            ) as connection:
                cursor = connection.execute(
                    _RENEW,
                    (self._lease_seconds, self._run_id, self._worker_id),
                )
                return cursor.rowcount == 1
        except psycopg.Error as exc:
            logger.warning("lease renewal failed: %s", type(exc).__name__)
            return None
