"""HTTP contract for the task API, backed by disposable PostgreSQL."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.database import stamp_task

from patchpilot.api import create_app
from patchpilot.config import get_settings
from patchpilot.db.models import DESCRIPTION_MAX_LENGTH, TITLE_MAX_LENGTH


@pytest.fixture
def client(postgres_database_url: str, clean_tasks: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


def post_task(
    client: TestClient,
    *,
    title: str = "Broken discount",
    description: str = "Unknown tier returns 500.",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/tasks",
        json={"title": title, "description": description},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_create_get_and_list_round_trip(client: TestClient):
    created = post_task(
        client,
        title="  Broken discount  ",
        description="  Unknown tier returns 500.  ",
    )

    assert created["title"] == "Broken discount"
    assert created["description"] == "Unknown tier returns 500."
    assert created["status"] == "queued"
    assert created["stage"] is None
    assert created["repo_id"] is None
    assert created["result_summary"] is None
    assert created["result_json"] is None
    created_at = datetime.fromisoformat(str(created["created_at"]))
    updated_at = datetime.fromisoformat(str(created["updated_at"]))
    assert created_at.tzinfo is not None
    assert updated_at.tzinfo is not None

    fetched = client.get(f"/api/v1/tasks/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created

    listed = client.get("/api/v1/tasks")
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1
    assert page["limit"] == 20
    assert page["offset"] == 0
    assert page["items"] == [created]


def test_task_survives_a_new_api_process(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as first:
        created = post_task(first, title="Survive restart", description="Still in PostgreSQL.")

    with TestClient(create_app()) as second:
        fetched = second.get(f"/api/v1/tasks/{created['id']}")

    assert fetched.status_code == 200
    assert fetched.json() == created


def test_list_paginates_newest_first_and_filters_status(
    client: TestClient,
    postgres_database_url: str,
):
    oldest = post_task(client, title="oldest", description="first report")
    middle = post_task(client, title="middle", description="second report")
    newest = post_task(client, title="newest", description="third report")
    stamp_task(
        postgres_database_url,
        oldest["id"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    stamp_task(
        postgres_database_url,
        middle["id"],
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        status="failed",
    )
    stamp_task(
        postgres_database_url,
        newest["id"],
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    first_page = client.get("/api/v1/tasks", params={"limit": 2, "offset": 0})
    assert first_page.status_code == 200
    body = first_page.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [item["id"] for item in body["items"]] == [newest["id"], middle["id"]]
    assert body["items"][1]["status"] == "failed"

    second_page = client.get("/api/v1/tasks", params={"limit": 2, "offset": 2})
    assert [item["id"] for item in second_page.json()["items"]] == [oldest["id"]]
    assert second_page.json()["total"] == 3

    queued = client.get("/api/v1/tasks", params={"status": "queued", "limit": 1})
    assert queued.status_code == 200
    assert queued.json()["total"] == 2
    assert [item["id"] for item in queued.json()["items"]] == [newest["id"]]

    failed = client.get("/api/v1/tasks", params={"status": "failed"})
    assert [item["id"] for item in failed.json()["items"]] == [middle["id"]]

    rejected = client.get("/api/v1/tasks", params={"status": "rejected"})
    assert rejected.json() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }

    past_end = client.get("/api/v1/tasks", params={"offset": 10})
    assert past_end.json()["items"] == []
    assert past_end.json()["total"] == 3


def test_unknown_task_is_404(client: TestClient):
    response = client.get(f"/api/v1/tasks/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_malformed_task_id_is_422(client: TestClient):
    response = client.get("/api/v1/tasks/not-a-uuid")

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": "Broken discount"},
        {"description": "Unknown tier returns 500."},
        {"title": "   ", "description": "Unknown tier returns 500."},
        {"title": "Broken discount", "description": "   "},
        {"title": "x" * (TITLE_MAX_LENGTH + 1), "description": "Unknown tier returns 500."},
        {"title": "Broken discount", "description": "y" * (DESCRIPTION_MAX_LENGTH + 1)},
        {
            "title": "Broken discount",
            "description": "Unknown tier returns 500.",
            "status": "completed",
            "stage": "triage",
            "repo_id": str(uuid4()),
            "result_summary": "done",
            "result_json": {"injected": True},
        },
    ],
)
def test_invalid_create_is_422_and_stores_nothing(client: TestClient, payload: dict[str, object]):
    response = client.post("/api/v1/tasks", json=payload)

    assert response.status_code == 422
    listed = client.get("/api/v1/tasks")
    assert listed.json()["total"] == 0


def test_create_accepts_maximum_lengths(client: TestClient):
    created = post_task(
        client,
        title="t" * TITLE_MAX_LENGTH,
        description="d" * DESCRIPTION_MAX_LENGTH,
    )

    assert created["title"] == "t" * TITLE_MAX_LENGTH
    assert created["description"] == "d" * DESCRIPTION_MAX_LENGTH


def test_title_is_stored_as_data_not_sql(client: TestClient):
    created = post_task(
        client,
        title="'; DROP TABLE tasks; --",
        description="still only a bug report",
    )

    fetched = client.get(f"/api/v1/tasks/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "'; DROP TABLE tasks; --"
    assert client.get("/api/v1/tasks").json()["total"] == 1


def test_invalid_json_is_422(client: TestClient):
    response = client.post(
        "/api/v1/tasks",
        content="{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"offset": -1}, "offset"),
        ({"offset": 10_001}, "offset"),
        ({"status": "not-a-status"}, "status"),
        ({"status": "QUEUED"}, "status"),
    ],
)
def test_invalid_list_query_is_422(client: TestClient, params: dict[str, object], field: str):
    response = client.get("/api/v1/tasks", params=params)

    assert response.status_code == 422
    locations = [tuple(error["loc"]) for error in response.json()["detail"]]
    assert any(field in location for location in locations)
