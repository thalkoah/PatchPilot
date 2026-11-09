from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app


def test_home_and_static_assets(client):
    assert client.get("/").status_code == 200
    assert "MiniTask" in client.get("/").text
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok"}


def test_empty_board(client):
    assert client.get("/api/tasks").json() == []


def test_create_and_list_task(client):
    result = client.post("/api/tasks", json={"title": "  First task  ", "description": "Details"})
    assert result.status_code == 201
    task = result.json()
    assert task["id"] > 0
    assert task["title"] == "First task"
    assert task["description"] == "Details"
    assert task["status"] == "todo"
    assert datetime.fromisoformat(task["created_at"])
    assert datetime.fromisoformat(task["updated_at"])
    assert client.get("/api/tasks").json() == [task]


def test_create_with_initial_status(client):
    result = client.post("/api/tasks", json={"title": "Working", "status": "in_progress"})
    assert result.status_code == 201
    assert result.json()["status"] == "in_progress"


def test_update_status_and_title(client):
    task = client.post("/api/tasks", json={"title": "Before"}).json()
    result = client.patch(
        f"/api/tasks/{task['id']}",
        json={"status": "done", "title": "After", "description": "Completed"},
    )
    assert result.status_code == 200
    updated = result.json()
    assert updated["status"] == "done"
    assert updated["title"] == "After"
    assert updated["description"] == "Completed"
    assert updated["created_at"] == task["created_at"]
    assert updated["updated_at"] >= task["updated_at"]
    assert client.get("/api/tasks").json() == [updated]


def test_update_only_changes_given_fields(client):
    task = client.post("/api/tasks", json={"title": "Keep", "description": "Keep this"}).json()
    updated = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"}).json()
    assert updated["title"] == "Keep"
    assert updated["description"] == "Keep this"


def test_delete_task(client):
    task = client.post("/api/tasks", json={"title": "Disposable"}).json()
    response = client.delete(f"/api/tasks/{task['id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/tasks").json() == []
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 404


def test_missing_task_returns_404(client):
    assert client.patch("/api/tasks/999", json={"status": "done"}).status_code == 404
    assert client.delete("/api/tasks/999").status_code == 404


def test_invalid_payloads_rejected(client):
    bad_creates = [
        {"title": ""},
        {"title": "   "},
        {"title": "X" * 121},
        {"title": "Valid", "status": "unknown"},
        {"title": "Valid", "unexpected": "data"},
    ]
    for payload in bad_creates:
        assert client.post("/api/tasks", json=payload).status_code == 422
    task = client.post("/api/tasks", json={"title": "Valid"}).json()
    for payload in [{}, {"status": None}, {"title": "   "}, {"description": None}, {"status": "not-real"}]:
        assert client.patch(f"/api/tasks/{task['id']}", json=payload).status_code == 422
    assert client.get("/api/tasks").json() == [task]


def test_newest_task_first(client):
    first = client.post("/api/tasks", json={"title": "First"}).json()
    second = client.post("/api/tasks", json={"title": "Second"}).json()
    assert [item["id"] for item in client.get("/api/tasks").json()] == [second["id"], first["id"]]


def test_database_survives_application_restart(client, tmp_path, monkeypatch):
    created = client.post("/api/tasks", json={"title": "Persistent"}).json()
    # The same path stays configured; another application lifespan reads the on-disk DB.
    with TestClient(app) as restarted_client:
        assert restarted_client.get("/api/tasks").json() == [created]
