"""Idle worker process. Job claiming and model calls start in a later task."""

import logging
import time

from patchpilot.config import get_settings

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("worker idle; no jobs configured")
    try:
        while True:
            time.sleep(settings.worker_idle_sleep_seconds)
    except KeyboardInterrupt:
        logger.info("worker stopped")


if __name__ == "__main__":
    main()
