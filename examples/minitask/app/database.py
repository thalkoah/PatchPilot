"""Small SQLite persistence layer, intentionally simple for PatchPilot exercises."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "minitask.db"


def db_path() -> Path:
    return Path(os.getenv("MINITASK_DB_PATH", str(DEFAULT_DB))).expanduser()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    database = db_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize() -> None:
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'todo'
                    CHECK(status IN ('todo', 'in_progress', 'done')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def list_tasks() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC, id DESC").fetchall()
        return [dict(row) for row in rows]


def get_task(task_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def create_task(title: str, description: str, status: str) -> dict:
    timestamp = utc_now()
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO tasks (title, description, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (title, description, status, timestamp, timestamp),
        )
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)


def update_task(task_id: int, changes: dict) -> dict | None:
    # Field names come exclusively from the validated API model, not user SQL.
    allowed_fields = {"title", "description", "status"}
    if not changes or not set(changes).issubset(allowed_fields):
        raise ValueError("Invalid update fields")

    assignments = ", ".join(f"{field} = ?" for field in changes)
    params = [*changes.values(), utc_now(), task_id]
    with connect() as db:
        cursor = db.execute(f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?", params)
        if cursor.rowcount == 0:
            return None
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row)


def delete_task(task_id: int) -> bool:
    with connect() as db:
        cursor = db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0
