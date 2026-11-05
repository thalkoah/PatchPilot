"""Audit events: bounded payloads, idempotent inserts, and the read API."""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.database import open_session_factory
from tests.workflow_support import compiled_worker_graph

from patchpilot.api import create_app
from patchpilot.audit import bound_payload
from patchpilot.config import get_settings
from patchpilot.repositories.events import append_event
from patchpilot.repositories.jobs import JobRepository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.worker import process_one
from patchpilot.workflow.graph import STAGE_TRIAGE

DEMO_TITLE = "Unknown customer tier crashes discount endpoint"
DEMO_DESCRIPTION = "An unrecognized customer tier returns HTTP 500."


def test_payload_redacts_secrets_and_caps_size():
    payload = bound_payload(
        {
            "attempt": 1,
            "password": "patchpilot_dev",
            "nested": {"api_key": "sk-live", "ok": True},
            "database_url": "postgresql://patchpilot:patchpilot_dev@db/patchpilot",
            "note": "token=abc " + ("x" * 5000),
            "items": list(range(50)),
        }
    )

    assert payload["attempt"] == 1
    assert payload["password"] == "[redacted]"
    assert payload["nested"]["api_key"] == "[redacted]"
    assert payload["nested"]["ok"] is True
    assert payload["database_url"] == "[redacted]"
    assert "patchpilot_dev" not in str(payload)
    assert "sk-live" not in str(payload)
    assert len(payload["note"]) <= 300
    assert len(payload["items"]) == 20


def test_non_object_payload_becomes_empty():
    assert bound_payload(["not", "an", "object"]) == {}
    assert bound_payload("secret") == {}


@pytest.fixture
def client(postgres_database_url: str, clean_tasks: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


async def test_duplicate_event_keeps_the_first_payload(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            created = await TaskRepository(session).create(
                title="Audit me",
                description="The first payload must win.",
            )
        key = f"{created.id}:-:{STAGE_TRIAGE}:stage_started"
        async with factory() as session:
            await append_event(
                session,
                task_id=created.id,
                run_id=None,
                stage=STAGE_TRIAGE,
                event_type="stage_started",
                payload={"attempt": 1, "note": "original"},
                idempotency_key=key,
                ordinal=0,
            )
            await session.commit()
        async with factory() as session:
            await append_event(
                session,
                task_id=created.id,
                run_id=None,
                stage=STAGE_TRIAGE,
                event_type="stage_started",
                payload={"attempt": 9, "note": "replacement"},
                idempotency_key=key,
                ordinal=0,
            )
            await session.commit()
    finally:
        await engine.dispose()

    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        rows = connection.execute(
            "SELECT payload FROM events WHERE task_id = %s",
            (created.id,),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0][0]["attempt"] == 1
    assert rows[0][0]["note"] == "original"


async def test_events_reject_update_and_delete(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            created = await TaskRepository(session).create(
                title="Keep the trail",
                description="A later edit must fail.",
            )
        async with factory() as session:
            await append_event(
                session,
                task_id=created.id,
                run_id=None,
                stage=STAGE_TRIAGE,
                event_type="stage_started",
                payload={"attempt": 1},
                idempotency_key=f"{created.id}:stage_started",
                ordinal=0,
            )
            await session.commit()
    finally:
        await engine.dispose()

    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute("UPDATE events SET stage = 'finalize'")
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute("DELETE FROM events")


async def test_only_one_open_run_is_allowed(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            created = await TaskRepository(session).create(
                title="One owner",
                description="A second open run must fail.",
            )
        async with factory() as session:
            claimed = await JobRepository(session).claim_next_queued(
                stage=STAGE_TRIAGE,
                worker_id="worker-a",
                lease_seconds=30,
            )
    finally:
        await engine.dispose()

    assert claimed is not None
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """
                INSERT INTO task_runs (id, task_id, attempt, worker_id, started_at, lease_until)
                VALUES (%s, %s, 2, 'worker-b', now(), now() + interval '30 seconds')
                """,
                (uuid4(), created.id),
            )


async def test_event_api_explains_a_completed_task(
    client: TestClient,
    postgres_database_url: str,
):
    created = client.post(
        "/api/v1/tasks",
        json={"title": DEMO_TITLE, "description": DEMO_DESCRIPTION},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    missing = client.get(f"/api/v1/tasks/{uuid4()}/events")
    assert missing.status_code == 404

    queued = client.get(f"/api/v1/tasks/{task_id}/events")
    assert queued.status_code == 200
    assert queued.json() == {"items": [], "truncated": False}

    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with compiled_worker_graph(postgres_database_url) as graph:
            assert await process_one(factory, _provider(), graph) is True
    finally:
        await engine.dispose()

    listed = client.get(f"/api/v1/tasks/{task_id}/events")
    assert listed.status_code == 200
    body = listed.json()
    assert body["truncated"] is False
    assert [item["event_type"] for item in body["items"]] == [
        "stage_started",
        "stage_completed",
        "route",
        "stage_started",
        "stage_completed",
        "task_completed",
    ]
    assert [item["stage"] for item in body["items"]] == [
        "triage",
        "triage",
        "supervisor",
        "finalize",
        "finalize",
        "finalize",
    ]
    route = body["items"][2]["payload"]
    assert route["route"] == "finalize"
    assert route["index"] == 1
    assert "repository" in route["reason"]
    assert body["items"][0]["payload"]["attempt"] == 1
    assert body["items"][-1]["payload"]["status"] == "completed"
    assert "idempotency_key" not in body["items"][0]
    for item in body["items"]:
        assert item["task_id"] == task_id
        assert "password" not in item["payload"]


def _provider():
    from patchpilot.llm.fake import FakeProvider

    return FakeProvider()
