"""The Alembic revision, not create_all, is what tests run against."""

import psycopg
from psycopg.rows import dict_row
from tests.database import upgrade_database

EXPECTED_COLUMNS = {
    "id",
    "title",
    "description",
    "repo_id",
    "status",
    "stage",
    "result_summary",
    "result_json",
    "created_at",
    "updated_at",
}


def test_upgrade_is_idempotent(postgres_database_url: str):
    upgrade_database(postgres_database_url)


def test_tasks_table_matches_the_contract(postgres_database_url: str):
    with psycopg.connect(
        postgres_database_url,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        columns = connection.execute(
            """
            SELECT column_name, is_nullable, data_type, udt_name, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'tasks'
            """
        ).fetchall()
        labels = connection.execute(
            """
            SELECT e.enumlabel
            FROM pg_enum AS e
            JOIN pg_type AS t ON t.oid = e.enumtypid
            WHERE t.typname = 'task_status'
            ORDER BY e.enumsortorder
            """
        ).fetchall()
        indexes = connection.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'tasks'
            """
        ).fetchall()
        constraints = connection.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'public.tasks'::regclass
            """
        ).fetchall()

    by_name = {row["column_name"]: row for row in columns}
    assert set(by_name) == EXPECTED_COLUMNS
    assert by_name["repo_id"]["is_nullable"] == "YES"
    assert by_name["stage"]["is_nullable"] == "YES"
    assert by_name["result_summary"]["is_nullable"] == "YES"
    assert by_name["result_json"]["is_nullable"] == "YES"
    assert by_name["result_json"]["udt_name"] == "jsonb"
    assert by_name["status"]["udt_name"] == "task_status"
    assert by_name["status"]["is_nullable"] == "NO"
    assert by_name["id"]["udt_name"] == "uuid"
    assert by_name["created_at"]["data_type"] == "timestamp with time zone"
    assert by_name["updated_at"]["data_type"] == "timestamp with time zone"
    assert by_name["created_at"]["column_default"] is not None
    assert by_name["updated_at"]["column_default"] is not None
    assert [row["enumlabel"] for row in labels] == [
        "queued",
        "running",
        "waiting_approval",
        "completed",
        "rejected",
        "failed",
        "cancelled",
    ]
    assert {row["indexname"] for row in indexes} >= {
        "tasks_pkey",
        "ix_tasks_created_at",
        "ix_tasks_status",
    }
    assert {row["conname"] for row in constraints} >= {
        "tasks_pkey",
        "ck_tasks_title_not_blank",
        "ck_tasks_description_not_blank",
        "ck_tasks_description_length",
    }
