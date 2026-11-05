"""LangGraph checkpoint tables in the application database.

`AsyncPostgresSaver.setup()` creates and migrates `checkpoint_migrations`,
`checkpoints`, `checkpoint_blobs`, and `checkpoint_writes`. Alembic does not
manage those tables. Business tasks stay on `tasks`.

The worker calls `setup_checkpoints` when it starts, then keeps one autocommit
connection open until the process stops. Run this module once yourself when
you want the tables before a worker is up:

    uv run python -m patchpilot.checkpoints
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import ValidationError

from patchpilot.config import get_settings
from patchpilot.logconfig import configure_logging

logger = logging.getLogger(__name__)

CHECKPOINTER_APPLICATION_NAME = "patchpilot_checkpointer"


def checkpointer_conninfo(database_url: str) -> str:
    """Return the libpq URL the saver opens. The password is unchanged."""
    if "application_name=" in database_url:
        return database_url
    join = "&" if "?" in database_url else "?"
    return f"{database_url}{join}application_name={CHECKPOINTER_APPLICATION_NAME}"


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Open the saver. The caller closes it by leaving this block."""
    async with AsyncPostgresSaver.from_conn_string(checkpointer_conninfo(database_url)) as saver:
        yield saver


async def setup_checkpoints(database_url: str) -> None:
    """Create or migrate LangGraph's tables. Safe to run more than once."""
    async with open_checkpointer(database_url) as saver:
        await saver.setup()


def main() -> int:
    configure_logging("INFO")
    try:
        settings = get_settings()
    except ValidationError:
        logger.error("checkpoint setup failed: invalid settings")
        return 2
    try:
        asyncio.run(setup_checkpoints(settings.database_url))
    except Exception as exc:
        logger.error("checkpoint setup failed: %s", type(exc).__name__)
        return 1
    logger.info("checkpoint tables ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
