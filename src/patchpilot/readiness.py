"""PostgreSQL readiness probe. The result is a boolean; connection details stay out of logs."""

import logging

import psycopg

from patchpilot.config import Settings

logger = logging.getLogger(__name__)


def database_is_ready(settings: Settings) -> bool:
    """Return True only when PostgreSQL answers `SELECT 1`."""
    try:
        with psycopg.connect(
            settings.database_url,
            autocommit=True,
            connect_timeout=settings.db_connect_timeout_seconds,
        ) as connection:
            row = connection.execute("SELECT 1").fetchone()
    except (psycopg.Error, OSError):
        logger.warning("database readiness check failed")
        return False
    return row is not None and row[0] == 1
