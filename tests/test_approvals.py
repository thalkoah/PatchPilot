"""Human approval pauses a code-changing plan and resumes the same thread."""

from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.database import open_session_factory
from tests.workflow_support import compiled_worker_graph

from patchpilot.api import create_app
from patchpilot.config import get_settings
from patchpilot.llm.fake import FakeProvider
from patchpilot.repositories.repos import RepoRepository
from patchpilot.worker import process_one

ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = ROOT / "examples"
DEMO_CHECKOUT = DEMO_ROOT / "demo_service"
DEMO_TITLE = "Unknown customer tier crashes discount endpoint"
DEMO_DESCRIPTION = (
    "When the demo-service discount endpoint receives a customer tier it does not "
    "recognize, it returns HTTP 500. It should respond with HTTP 422."
)
APPROVE_REASON = "Apply the validation fix."
REJECT_REASON = "The proposed edit is wider than the bug."


@pytest.fixture
def artifacts_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


@pytest.fixture
def client(
    postgres_database_url: str,
    clean_tasks: None,
    artifacts_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    monkeypatch.setenv("ARTIFACTS_ROOT", str(artifacts_root))
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


async def test_approve_survives_restart_and_a_repeated_decision(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
):
    before = _snapshot(DEMO_CHECKOUT)
    provider = FakeProvider()
    task_id, calls = await _pause_demo(client, postgres_database_url, artifacts_root, provider)
    assert _snapshot(DEMO_CHECKOUT) == before
    assert _checkpoint_count(postgres_database_url, task_id) >= 1

    waiting = client.get(f"/api/v1/tasks/{task_id}")
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "waiting_approval"
    assert waiting.json()["stage"] == "request_plan_approval"
    approvals = client.get(f"/api/v1/tasks/{task_id}/approvals")
    assert approvals.status_code == 200
    item = approvals.json()["items"][0]
    assert item["status"] == "pending"
    assert item["plan_version"] == 1
    assert item["reason"] is None
    plan = client.get(f"/api/v1/tasks/{task_id}/plan")
    assert plan.status_code == 200
    assert plan.json()["plan"]["can_proceed"] is True
    assert not (DEMO_CHECKOUT / "tests/test_unknown_tier_added.py").exists()

    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with compiled_worker_graph(postgres_database_url) as graph:
            assert (
                await process_one(
                    factory,
                    provider,
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is False
            )
            first = _decide(client, task_id, item["id"], "approve", APPROVE_REASON)
            assert first["status"] == "approved"
            assert first["reason"] == APPROVE_REASON
            second = _decide(client, task_id, item["id"], "approve", "A different reason")
            assert second["id"] == first["id"]
            assert second["reason"] == APPROVE_REASON
            conflict = client.post(
                f"/api/v1/tasks/{task_id}/decisions",
                json={
                    "approval_id": item["id"],
                    "decision": "reject",
                    "reason": REJECT_REASON,
                },
            )
            assert conflict.status_code == 409
            assert (
                await process_one(
                    factory,
                    provider,
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is True
            )
            assert (
                await process_one(
                    factory,
                    provider,
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is False
            )
            replay = _decide(client, task_id, item["id"], "approve", "Again")
            assert replay["status"] == "approved"
            assert replay["reason"] == APPROVE_REASON
    finally:
        await engine.dispose()

    assert len(provider.calls) == calls
    done = client.get(f"/api/v1/tasks/{task_id}")
    body = done.json()
    assert body["status"] == "completed"
    assert body["stage"] == "finalize"
    assert body["result_json"]["approval"]["decision"] == "approve"
    assert body["result_json"]["approval"]["reason"] == APPROVE_REASON
    assert _snapshot(DEMO_CHECKOUT) == before
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()["items"]
    kinds = [item["event_type"] for item in events]
    assert kinds.count("approval_requested") == 1
    assert kinds.count("approval_decided") == 1
    assert kinds.count("task_completed") == 1
    assert "task_rejected" not in kinds
    decided = next(item for item in events if item["event_type"] == "approval_decided")
    assert decided["payload"]["decision"] == "approve"
    assert decided["payload"]["reason"] == APPROVE_REASON
    assert _approval_count(postgres_database_url, task_id) == 1


async def test_reject_is_terminal_and_a_second_decision_does_not_resume(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
):
    before = _snapshot(DEMO_CHECKOUT)
    provider = FakeProvider()
    task_id, calls = await _pause_demo(client, postgres_database_url, artifacts_root, provider)
    approval_id = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["items"][0]["id"]

    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with compiled_worker_graph(postgres_database_url) as graph:
            recorded = _decide(client, task_id, approval_id, "reject", REJECT_REASON)
            assert recorded["status"] == "rejected"
            assert recorded["reason"] == REJECT_REASON
            again = _decide(client, task_id, approval_id, "reject", "Say it twice")
            assert again["reason"] == REJECT_REASON
            conflict = client.post(
                f"/api/v1/tasks/{task_id}/decisions",
                json={"approval_id": approval_id, "decision": "approve"},
            )
            assert conflict.status_code == 409
            assert (
                await process_one(
                    factory,
                    provider,
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is True
            )
            assert (
                await process_one(
                    factory,
                    provider,
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is False
            )
    finally:
        await engine.dispose()

    assert len(provider.calls) == calls
    body = client.get(f"/api/v1/tasks/{task_id}").json()
    assert body["status"] == "rejected"
    assert body["result_summary"] == REJECT_REASON
    assert body["result_json"]["approval"]["decision"] == "reject"
    assert "error" not in body["result_json"]
    assert _snapshot(DEMO_CHECKOUT) == before
    kinds = [
        item["event_type"] for item in client.get(f"/api/v1/tasks/{task_id}/events").json()["items"]
    ]
    assert kinds.count("approval_decided") == 1
    assert kinds.count("task_rejected") == 1
    assert "task_completed" not in kinds
    assert _approval_count(postgres_database_url, task_id) == 1


async def test_a_declined_plan_does_not_ask_for_approval(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
):
    before = _snapshot(DEMO_CHECKOUT)
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            repository = await RepoRepository(session).save(
                display_name="Demo service",
                canonical_path="demo_service",
                registration={"head": "0" * 40},
            )
        created = client.post(
            "/api/v1/tasks",
            json={
                "title": DEMO_TITLE,
                "description": "This report is ambiguous and names no single failing request.",
                "repo_id": str(repository.id),
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        async with compiled_worker_graph(postgres_database_url) as graph:
            assert (
                await process_one(
                    factory,
                    FakeProvider(),
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is True
            )
    finally:
        await engine.dispose()

    body = client.get(f"/api/v1/tasks/{task_id}").json()
    assert body["status"] == "completed"
    assert body["result_json"]["plan"]["can_proceed"] is False
    assert "approval" not in body["result_json"]
    listed = client.get(f"/api/v1/tasks/{task_id}/approvals")
    assert listed.status_code == 200
    assert listed.json() == {"items": []}
    assert _snapshot(DEMO_CHECKOUT) == before


def test_missing_and_invalid_decisions(client: TestClient):
    missing = client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000000/approvals")
    assert missing.status_code == 404
    created = client.post(
        "/api/v1/tasks",
        json={"title": "Save fails", "description": "The button fails."},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    assert client.get(f"/api/v1/tasks/{task_id}/approvals").json() == {"items": []}
    unknown = client.post(
        f"/api/v1/tasks/{task_id}/decisions",
        json={
            "approval_id": "00000000-0000-0000-0000-000000000000",
            "decision": "approve",
        },
    )
    assert unknown.status_code == 404
    invalid = client.post(
        f"/api/v1/tasks/{task_id}/decisions",
        json={"approval_id": task_id, "decision": "ship"},
    )
    assert invalid.status_code == 422
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "queued"


async def _pause_demo(
    client: TestClient,
    database_url: str,
    artifacts_root: Path,
    provider: FakeProvider,
) -> tuple[str, int]:
    engine, factory = open_session_factory(database_url)
    try:
        async with factory() as session:
            repository = await RepoRepository(session).save(
                display_name="Demo service",
                canonical_path="demo_service",
                registration={"head": "0" * 40},
            )
        created = client.post(
            "/api/v1/tasks",
            json={
                "title": DEMO_TITLE,
                "description": DEMO_DESCRIPTION,
                "repo_id": str(repository.id),
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        async with compiled_worker_graph(database_url) as graph:
            assert (
                await process_one(
                    factory,
                    provider,
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is True
            )
    finally:
        await engine.dispose()
    return task_id, len(provider.calls)


def _decide(
    client: TestClient,
    task_id: str,
    approval_id: str,
    decision: str,
    reason: str,
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/tasks/{task_id}/decisions",
        json={"approval_id": approval_id, "decision": decision, "reason": reason},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _checkpoint_count(database_url: str, task_id: str) -> int:
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s",
            (task_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _approval_count(database_url: str, task_id: str) -> int:
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT count(*) FROM approvals WHERE task_id = %s",
            (task_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _snapshot(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if any(part in {"__pycache__", ".git", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found
