"""Disposable PostgreSQL for tests that must not use SQLite or the Compose volume."""

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from patchpilot.db.session import sqlalchemy_url

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "docker.io/library/postgres:16-alpine"
READY_TIMEOUT_SECONDS = 60


def upgrade_database(database_url: str) -> None:
    """Apply Alembic migrations to one database and restore the process environment."""
    config = Config(str(ROOT / "alembic.ini"))
    previous = os.environ.get("DATABASE_URL")
    previous_quiet = os.environ.get("PATCHPILOT_ALEMBIC_QUIET")
    os.environ["DATABASE_URL"] = database_url
    os.environ["PATCHPILOT_ALEMBIC_QUIET"] = "1"
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        if previous_quiet is None:
            os.environ.pop("PATCHPILOT_ALEMBIC_QUIET", None)
        else:
            os.environ["PATCHPILOT_ALEMBIC_QUIET"] = previous_quiet


def container_runtime() -> str:
    for candidate in ("podman", "docker"):
        found = shutil.which(candidate)
        if found is not None:
            return found
    raise RuntimeError(
        "Disposable PostgreSQL tests need podman or docker on PATH. "
        "SQLite is not a substitute for this suite."
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(database_url: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(database_url, connect_timeout=2, autocommit=True) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            last_error = exc
            time.sleep(0.4)
    raise RuntimeError(f"disposable PostgreSQL did not accept connections: {last_error}")


@contextmanager
def disposable_postgres() -> Iterator[str]:
    """Start postgres:16, migrate it, and remove the container on exit."""
    runtime = container_runtime()
    name = f"patchpilot-pytest-pg-{uuid4().hex[:8]}"
    port = _free_port()
    started = subprocess.run(
        [
            runtime,
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--publish",
            f"127.0.0.1:{port}:5432",
            "--env",
            "POSTGRES_USER=patchpilot",
            "--env",
            "POSTGRES_PASSWORD=patchpilot_dev",
            "--env",
            "POSTGRES_DB=patchpilot",
            IMAGE,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if started.returncode != 0:
        raise RuntimeError(
            "failed to start disposable PostgreSQL\n"
            f"{started.stderr.strip() or started.stdout.strip()}"
        )
    database_url = f"postgresql://patchpilot:patchpilot_dev@127.0.0.1:{port}/patchpilot"
    try:
        try:
            _wait_until_ready(database_url)
        except RuntimeError:
            logs = subprocess.run(
                [runtime, "logs", name],
                check=False,
                capture_output=True,
                text=True,
            )
            detail = (logs.stderr or logs.stdout or "").strip()
            raise RuntimeError(f"disposable PostgreSQL was not ready\n{detail}") from None
        upgrade_database(database_url)
        yield database_url
    finally:
        subprocess.run(
            [runtime, "rm", "--force", name],
            check=False,
            capture_output=True,
            text=True,
        )


def stamp_task(
    database_url: str,
    task_id: UUID | str,
    *,
    created_at: datetime,
    status: str | None = None,
) -> None:
    """Set stored ordering fields directly. The API does not expose status changes yet."""
    stored_id = task_id if isinstance(task_id, UUID) else UUID(task_id)
    with psycopg.connect(database_url, autocommit=True) as connection:
        if status is None:
            connection.execute(
                "UPDATE tasks SET created_at = %s WHERE id = %s",
                (created_at, stored_id),
            )
            return
        connection.execute(
            "UPDATE tasks SET created_at = %s, status = CAST(%s AS task_status) WHERE id = %s",
            (created_at, status, stored_id),
        )


def fetch_status(database_url: str, task_id: UUID) -> str:
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT status::text FROM tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"task {task_id} was not stored")
    return str(row[0])


def truncate_tasks(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE tasks CASCADE")


def expire_open_leases(database_url: str) -> None:
    """Move every open lease into the past so the next worker may recover it."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE task_runs
            SET lease_until = now() - interval '1 second'
            WHERE ended_at IS NULL
            """
        )


def open_session_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(sqlalchemy_url(database_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory
