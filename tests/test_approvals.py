"""Human approval pauses a code-changing plan and resumes the same thread."""

import json
import subprocess
from pathlib import Path
from uuid import UUID

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
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
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
    workspace_root: Path,
    tmp_path: Path,
):
    from tests.test_workspace import copy_demo_repository, git_snapshot

    repo_root = copy_demo_repository(tmp_path / "repos")
    checkout = repo_root / "demo_service"
    before = git_snapshot(checkout)
    provider = FakeProvider()
    task_id, calls = await _pause_demo(
        client,
        postgres_database_url,
        artifacts_root,
        provider,
        repository_root=repo_root,
        workspace_root=workspace_root,
    )
    assert git_snapshot(checkout) == before
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
                    repository_root=repo_root,
                    artifacts_root=artifacts_root,
                    workspace_root=workspace_root,
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
                    repository_root=repo_root,
                    artifacts_root=artifacts_root,
                    workspace_root=workspace_root,
                )
                is True
            )
            assert (
                await process_one(
                    factory,
                    provider,
                    graph,
                    repository_root=repo_root,
                    artifacts_root=artifacts_root,
                    workspace_root=workspace_root,
                )
                is False
            )
            replay = _decide(client, task_id, item["id"], "approve", "Again")
            assert replay["status"] == "approved"
            assert replay["reason"] == APPROVE_REASON
    finally:
        await engine.dispose()

    assert len(provider.calls) == calls + 1
    assert provider.calls[-1].schema_name == "PatchProposal"
    done = client.get(f"/api/v1/tasks/{task_id}")
    body = done.json()
    assert body["status"] == "completed"
    assert body["stage"] == "finalize"
    assert body["result_json"]["approval"]["decision"] == "approve"
    assert body["result_json"]["approval"]["reason"] == APPROVE_REASON
    assert body["result_json"]["workspace"]["branch"] == f"patchpilot/{task_id}"
    assert body["result_json"]["workspace"]["base_sha"] == _rev_parse(checkout)
    assert "clone_path" not in body["result_json"]["workspace"]
    assert str(workspace_root) not in done.text
    assert str(checkout) not in done.text
    assert git_snapshot(checkout) == before
    assert not (checkout / ".git" / "worktrees").exists()
    worktree = workspace_root / task_id / "worktree"
    assert (worktree / "src/demo_service/discounts.py").is_file()
    assert "UnknownTierError" in (worktree / "src/demo_service/discounts.py").read_text()
    assert "UnknownTierError" not in (checkout / "src/demo_service/discounts.py").read_text()
    assert (worktree / "tests/test_unknown_tier_added.py").is_file()
    assert not (checkout / "tests/test_unknown_tier_added.py").exists()
    patch_meta = body["result_json"]["patch"]
    assert patch_meta["checksum"] != patch_meta["proposal_checksum"]
    assert "diff --git" not in done.text
    listed = client.get(f"/api/v1/tasks/{task_id}/patch")
    assert listed.status_code == 200
    patch_body = listed.json()
    assert patch_body["checksum"] == patch_meta["checksum"]
    assert patch_body["proposal_checksum"] == patch_meta["proposal_checksum"]
    assert "diff --git a/src/demo_service/discounts.py" in patch_body["diff"]
    assert patch_body["proposal"]["edits"]
    assert patch_body["diff"] not in json.dumps(patch_body["proposal"])
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()["items"]
    kinds = [item["event_type"] for item in events]
    assert kinds.count("approval_requested") == 1
    assert kinds.count("approval_decided") == 1
    assert kinds.count("task_completed") == 1
    assert "task_rejected" not in kinds
    decided = next(item for item in events if item["event_type"] == "approval_decided")
    assert decided["payload"]["decision"] == "approve"
    assert decided["payload"]["reason"] == APPROVE_REASON
    prepared = [item for item in events if item["stage"] == "prepare_workspace"]
    assert [item["event_type"] for item in prepared] == ["stage_started", "stage_completed"]
    developed = [item for item in events if item["stage"] == "develop"]
    assert [item["event_type"] for item in developed] == ["stage_started", "stage_completed"]
    assert developed[1]["payload"]["checksum"] == patch_meta["checksum"]
    assert developed[1]["payload"]["proposal_checksum"] == patch_meta["proposal_checksum"]
    assert "diff --git" not in json.dumps(developed)
    assert prepared[1]["payload"]["base_sha"] == body["result_json"]["workspace"]["base_sha"]
    assert prepared[1]["payload"]["branch"] == f"patchpilot/{task_id}"
    assert str(workspace_root) not in json.dumps(prepared)
    assert _approval_count(postgres_database_url, task_id) == 1
    stored = _workspace_row(postgres_database_url, task_id)
    assert stored == (f"{task_id}/clone", f"{task_id}/worktree", "active")
    await _retire(postgres_database_url, task_id, workspace_root)
    assert git_snapshot(checkout) == before
    assert not (workspace_root / task_id).exists()
    assert _workspace_row(postgres_database_url, task_id)[2] == "removed"


async def test_reject_is_terminal_and_a_second_decision_does_not_resume(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
):
    before = _snapshot(DEMO_CHECKOUT)
    provider = FakeProvider()
    task_id, calls = await _pause_demo(
        client,
        postgres_database_url,
        artifacts_root,
        provider,
        workspace_root=workspace_root,
    )
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
                    workspace_root=workspace_root,
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
                    workspace_root=workspace_root,
                )
                is False
            )
    finally:
        await engine.dispose()

    assert len(provider.calls) == calls
    body = client.get(f"/api/v1/tasks/{task_id}").json()
    assert body["status"] == "rejected"
    assert "workspace" not in body["result_json"]
    assert list(workspace_root.iterdir()) == []
    assert _workspace_row(postgres_database_url, task_id) is None
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
    workspace_root: Path,
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
                    workspace_root=workspace_root,
                )
                is True
            )
    finally:
        await engine.dispose()

    body = client.get(f"/api/v1/tasks/{task_id}").json()
    assert body["status"] == "completed"
    assert body["result_json"]["plan"]["can_proceed"] is False
    assert "workspace" not in body["result_json"]
    assert list(workspace_root.iterdir()) == []
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
    *,
    repository_root: Path | None = None,
    workspace_root: Path | None = None,
) -> tuple[str, int]:
    checkout_root = DEMO_ROOT.resolve() if repository_root is None else repository_root
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
                    repository_root=checkout_root,
                    artifacts_root=artifacts_root,
                    workspace_root=workspace_root,
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


def _rev_parse(checkout: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _workspace_row(database_url: str, task_id: str) -> tuple[str, str, str] | None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            """
            SELECT clone_path, worktree_path, status::text
            FROM workspaces
            WHERE task_id = %s
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    return (str(row[0]), str(row[1]), str(row[2]))


async def _retire(database_url: str, task_id: str, workspace_root: Path) -> None:
    from patchpilot.db.models import WorkspaceStatus
    from patchpilot.repositories.workspaces import WorkspaceRepository

    engine, factory = open_session_factory(database_url)
    try:
        async with factory() as session:
            row = await WorkspaceRepository(session).retire(UUID(task_id), workspace_root)
            assert row is not None
            assert row.status == WorkspaceStatus.REMOVED
        async with factory() as session:
            again = await WorkspaceRepository(session).retire(UUID(task_id), workspace_root)
            assert again is not None
            assert again.status == WorkspaceStatus.REMOVED
    finally:
        await engine.dispose()


def _snapshot(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if any(part in {"__pycache__", ".git", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found
