"""The reviewer is advisory. Policy allows at most two repairs."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.database import open_session_factory
from tests.runner_support import start_runner, stop_runner
from tests.test_approvals import DEMO_DESCRIPTION, _decide, _pause_demo
from tests.test_workspace import copy_demo_repository, git_snapshot
from tests.workflow_support import compiled_worker_graph

from patchpilot.agents.limits import require_repair_budget
from patchpilot.agents.review_policy import (
    CRITICAL_REASON,
    PATCH_POLICY_REASON,
    REPAIR_LIMIT_REASON,
    TEST_LIMIT_REASON,
    PolicyAction,
    applied_diff_is_allowed,
    decide_review,
)
from patchpilot.agents.reviewer import ReviewerAgent
from patchpilot.api import create_app
from patchpilot.config import get_settings
from patchpilot.llm.fake import ACCEPT_REVIEW, DEMO_CHANGE_PLAN, FakeProvider
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.review import FindingSeverity, ReviewAction, ReviewFinding, ReviewResult
from patchpilot.worker import process_one

_DOCKER_DIFF = (
    "diff --git a/Dockerfile b/Dockerfile\n"
    "--- a/Dockerfile\n"
    "+++ b/Dockerfile\n"
    "@@ -1 +1 @@\n"
    "-FROM python\n"
    "+FROM scratch\n"
)


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


def test_policy_blocks_a_failing_suite_and_a_forbidden_diff():
    repair = decide_review(
        review=ACCEPT_REVIEW,
        tests_passed=False,
        patch_allowed=True,
        repair_count=0,
    )
    assert repair.action is PolicyAction.REPAIR
    assert repair.reason.startswith("Tests failed.")

    exhausted = decide_review(
        review=ACCEPT_REVIEW,
        tests_passed=False,
        patch_allowed=True,
        repair_count=2,
    )
    assert exhausted.action is PolicyAction.FAIL
    assert exhausted.reason.startswith(TEST_LIMIT_REASON)
    assert "person needs to intervene" in exhausted.reason

    blocked = decide_review(
        review=ACCEPT_REVIEW,
        tests_passed=True,
        patch_allowed=False,
        repair_count=0,
    )
    assert blocked.action is PolicyAction.FAIL
    assert blocked.reason.startswith(PATCH_POLICY_REASON)
    assert applied_diff_is_allowed(_DOCKER_DIFF, DEMO_CHANGE_PLAN) is False


def test_policy_stops_after_two_repairs_and_a_critical_accept_fails():
    limited = decide_review(
        review=ReviewResult(
            summary="The patch still needs another revision.",
            action=ReviewAction.REPAIR,
            findings=[],
        ),
        tests_passed=True,
        patch_allowed=True,
        repair_count=2,
    )
    assert limited.action is PolicyAction.FAIL
    assert limited.reason.startswith(REPAIR_LIMIT_REASON)

    critical = decide_review(
        review=ReviewResult(
            summary="Do not ship this.",
            action=ReviewAction.ACCEPT,
            findings=[
                ReviewFinding(
                    summary="A secret would be written.",
                    severity=FindingSeverity.CRITICAL,
                    evidence="src/demo_service/discounts.py",
                )
            ],
        ),
        tests_passed=True,
        patch_allowed=True,
        repair_count=0,
    )
    assert critical.action is PolicyAction.FAIL
    assert critical.reason.startswith(CRITICAL_REASON)
    assert require_repair_budget(2) == 2
    with pytest.raises(ValueError):
        require_repair_budget(3)


def test_reviewer_drops_an_invented_path_and_does_not_edit():
    from patchpilot.runner.protocol import TestReport, TestStatus

    report = TestReport(
        job_id=uuid4(),
        status=TestStatus.PASSED,
        exit_code=0,
        timed_out=False,
        duration_ms=4,
        stdout="passed",
    )
    provider = FakeProvider()
    verdict = ReviewerAgent(provider).run(
        title="Unknown tier",
        description="review bogus evidence mentions invented/not_read.py",
        plan=DEMO_CHANGE_PLAN,
        report=InvestigationReport(
            summary="The lookup indexes KNOWN_TIERS.",
            hypotheses=[],
            evidence=[],
            probable_files=[],
            unanswered_questions=[],
        ),
        diff="diff --git a/src/demo_service/discounts.py b/src/demo_service/discounts.py\n",
        tests=report,
        repair_count=0,
    )

    dumped = verdict.model_dump_json()
    assert "invented/not_read.py" not in dumped
    assert provider.calls[0].schema_name == "ReviewResult"
    assert "Do not edit files" in provider.calls[0].prompt
    assert "diff --git a/src/demo_service/discounts.py" in provider.calls[0].prompt
    assert "Tests status: passed" in provider.calls[0].prompt


async def test_one_repair_then_accepts_the_tested_diff(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
):
    finished = await _finish(
        client,
        postgres_database_url,
        artifacts_root,
        workspace_root,
        tmp_path,
        f"{DEMO_DESCRIPTION}\n\nreview repair once",
    )

    assert finished.body["status"] == "completed"
    assert finished.body["stage"] == "finalize"
    assert finished.body["result_json"]["tests"]["passed"] is True
    assert finished.body["result_json"]["review"]["action"] == "accept"
    assert finished.body["result_json"]["review"]["policy"] == "accept"
    assert finished.body["result_json"]["review"]["repair_count"] == 1
    assert finished.names.count("PatchProposal") == 2
    assert finished.names.count("ReviewResult") == 2
    assert len(finished.repairs) == 1
    assert finished.repairs[0]["payload"]["index"] == 1
    assert finished.repairs[0]["payload"]["reason"].startswith("Tests failed.")
    assert "diff --git" not in finished.repairs[0]["payload"]["reason"]
    first = client.get(f"/api/v1/tasks/{finished.task_id}/patch?version=1")
    second = client.get(f"/api/v1/tasks/{finished.task_id}/patch?version=2")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["checksum"] != second.json()["checksum"]
    assert first.json()["checksum"] != first.json()["proposal_checksum"]
    assert second.json()["checksum"] != second.json()["proposal_checksum"]
    assert "UnknownTierError" not in first.json()["diff"]
    assert "UnknownTierError" in second.json()["diff"]
    reviewed = client.get(f"/api/v1/tasks/{finished.task_id}/review")
    assert reviewed.status_code == 200
    assert reviewed.json()["policy"] == "accept"
    assert "diff --git" not in reviewed.text
    assert finished.snapshot == finished.before
    source_discount = finished.checkout / "src/demo_service/discounts.py"
    assert "UnknownTierError" not in source_discount.read_text()
    worktree = workspace_root / finished.task_id / "worktree" / "src/demo_service/discounts.py"
    assert "UnknownTierError" in worktree.read_text()


async def test_failed_tests_do_not_count_as_success(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
):
    finished = await _finish(
        client,
        postgres_database_url,
        artifacts_root,
        workspace_root,
        tmp_path,
        f"{DEMO_DESCRIPTION}\n\nreview tests fail",
    )

    assert finished.body["status"] == "failed"
    assert finished.body["stage"] == "review"
    assert finished.body["result_json"]["tests"]["passed"] is False
    assert finished.body["result_json"]["review"]["action"] == "accept"
    assert finished.body["result_json"]["review"]["policy"] == "fail"
    assert "Tests failed and the repair limit is exhausted" in finished.body["result_summary"]
    assert "person needs to intervene" in finished.body["result_summary"]
    assert finished.names.count("PatchProposal") == 3
    assert finished.names.count("ReviewResult") == 3
    assert len(finished.repairs) == 2
    assert [item["payload"]["index"] for item in finished.repairs] == [1, 2]
    assert finished.snapshot == finished.before
    assert not (workspace_root / finished.task_id).exists()


async def test_repeated_reviewer_rejection_stops_at_two_repairs(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
):
    finished = await _finish(
        client,
        postgres_database_url,
        artifacts_root,
        workspace_root,
        tmp_path,
        f"{DEMO_DESCRIPTION}\n\nreview reject",
    )

    assert finished.body["status"] == "failed"
    assert finished.body["result_json"]["review"]["critical"] is True
    assert finished.body["result_json"]["review"]["action"] == "repair"
    assert finished.body["result_json"]["review"]["policy"] == "fail"
    assert "A critical review finding blocks the patch" in finished.body["result_summary"]
    assert "person needs to intervene" in finished.body["result_summary"]
    assert finished.names.count("PatchProposal") == 3
    assert len(finished.repairs) == 2
    assert finished.names.count("ReviewResult") == 3
    assert finished.snapshot == finished.before
    assert not (workspace_root / finished.task_id).exists()


async def test_repair_limit_is_exhausted_when_the_reviewer_keeps_asking(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
):
    finished = await _finish(
        client,
        postgres_database_url,
        artifacts_root,
        workspace_root,
        tmp_path,
        f"{DEMO_DESCRIPTION}\n\nreview forever",
    )

    assert finished.body["status"] == "failed"
    assert finished.body["result_json"]["tests"]["passed"] is True
    assert finished.body["result_json"]["review"]["critical"] is False
    assert finished.body["result_json"]["review"]["action"] == "repair"
    assert "Repair stopped after 2 attempts" in finished.body["result_summary"]
    assert "person needs to intervene" in finished.body["result_summary"]
    assert finished.names.count("PatchProposal") == 3
    assert finished.names.count("ReviewResult") == 3
    assert len(finished.repairs) == 2
    newest = client.get(f"/api/v1/tasks/{finished.task_id}/patch")
    assert newest.status_code == 200
    assert newest.json()["version"] == 3
    assert newest.json()["checksum"] != newest.json()["proposal_checksum"]
    reviewed = client.get(f"/api/v1/tasks/{finished.task_id}/review")
    assert reviewed.json()["checksum"] != newest.json()["checksum"]
    assert "diff --git" not in reviewed.text
    assert finished.snapshot == finished.before
    assert not (workspace_root / finished.task_id).exists()


async def _finish(
    client: TestClient,
    database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
    description: str,
) -> SimpleNamespace:
    repo_root = copy_demo_repository(tmp_path / "repos")
    checkout = repo_root / "demo_service"
    before = git_snapshot(checkout)
    provider = FakeProvider()
    task_id, calls = await _pause_demo(
        client,
        database_url,
        artifacts_root,
        provider,
        repository_root=repo_root,
        workspace_root=workspace_root,
        description=description,
    )
    approval_id = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["items"][0]["id"]
    engine, factory = open_session_factory(database_url)
    runner = start_runner(tmp_path / "runner", timeout=20, poll=0.05)
    try:
        async with compiled_worker_graph(database_url) as graph:
            _decide(client, task_id, approval_id, "approve", "Apply the validation fix.")
            assert (
                await process_one(
                    factory,
                    provider,
                    graph,
                    repository_root=repo_root,
                    artifacts_root=artifacts_root,
                    workspace_root=workspace_root,
                    runner_root=tmp_path / "runner",
                    runner_wait_seconds=35,
                    database_url=database_url,
                )
                is True
            )
    finally:
        await engine.dispose()
        stop_runner(runner)
    body = client.get(f"/api/v1/tasks/{task_id}").json()
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()["items"]
    return SimpleNamespace(
        task_id=task_id,
        body=body,
        repairs=[item for item in events if item["event_type"] == "repair"],
        names=[call.schema_name for call in provider.calls[calls:]],
        before=before,
        snapshot=git_snapshot(checkout),
        checkout=checkout,
    )
