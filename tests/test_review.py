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

from patchpilot.agents.developer import DeveloperAgent
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
from patchpilot.db.models import TaskStatus
from patchpilot.llm.fake import ACCEPT_REVIEW, DEMO_CHANGE_PLAN, FakeProvider
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.patch import FileEdit, PatchProposal
from patchpilot.llm.plan import ChangePlan, ModificationKind, ProposedModification
from patchpilot.llm.review import FindingSeverity, ReviewAction, ReviewFinding, ReviewResult
from patchpilot.llm.triage import Classification, Severity, TriageResult
from patchpilot.patching.errors import PatchError
from patchpilot.runner.client import test_job_id as _job_id
from patchpilot.runner.protocol import TestReport as _TestReport
from patchpilot.runner.protocol import TestStatus as _TestStatus
from patchpilot.worker import process_one
from patchpilot.workflow.graph import (
    EMPTY_PATCH,
    REVIEW_FAILURE,
    STAGE_DEVELOP,
    STAGE_FINALIZE,
    STAGE_REVIEW,
    STAGE_TRIAGE,
    _failed_update,
    finalize_node,
    outcome_from_state,
)
from patchpilot.workspace.layout import (
    IsolatedWorkspace,
    branch_name,
    clone_relative,
    worktree_relative,
)

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
    report = _TestReport(
        job_id=uuid4(),
        status=_TestStatus.PASSED,
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


def test_repair_prompt_names_the_previous_attempt_and_the_failure(tmp_path: Path):
    root = tmp_path / "work"
    target = root / "src" / "demo_service"
    target.mkdir(parents=True)
    (target / "app.py").write_text('app = FastAPI(title="Test service")\n', encoding="utf-8")
    finding = "The suite still returns HTTP 500."
    excerpt = "assert 500 == 422\n" + ("y" * 600)
    review = ReviewResult(
        summary="Tests failed after the title change.",
        action=ReviewAction.REPAIR,
        findings=[
            ReviewFinding(
                summary=finding,
                severity=FindingSeverity.HIGH,
                evidence="assert 500 == 422",
            )
        ],
    )
    report = _TestReport(
        job_id=uuid4(),
        status=_TestStatus.FAILED,
        exit_code=1,
        timed_out=False,
        duration_ms=20,
        stdout=excerpt,
    )
    provider = _PromptRecorder(_proposal())
    with pytest.raises(PatchError):
        DeveloperAgent(provider, root).run(
            title="rename the service",
            description="Change the title to Test service.",
            plan=_title_plan(),
            report=InvestigationReport(
                summary="The title is in app.py.",
                hypotheses=[],
                evidence=[],
                probable_files=["src/demo_service/app.py"],
                unanswered_questions=[],
            ),
            review=review,
            repair_count=1,
            tests=report,
        )

    prompt = provider.prompts[0]
    assert "The files below are the previous attempt." in prompt
    assert "An unchanged file is rejected." in prompt
    assert "The next edit must stay on the approved paths." in prompt
    assert finding in prompt
    captured = excerpt.strip()[:500]
    marker = "Test output:\n"
    start = prompt.index(marker) + len(marker)
    assert prompt[start : start + 500] == captured
    assert len(captured) == 500
    assert captured.startswith("assert 500 == 422\n")
    assert "y" * 600 not in prompt
    assert prompt[start + 500] == "\n"


def test_empty_repair_keeps_the_patch_and_the_review():
    task_id = uuid4()
    second = _diff("second")
    cycles = [
        _cycle(task_id, 0, passed=False, action=ReviewAction.REPAIR, diff=_diff("first")),
        _cycle(task_id, 1, passed=False, action=ReviewAction.REPAIR, diff=second),
    ]
    state = _reviewed_state(task_id, cycles, diff=second, status="running")
    triage = state["triage"]
    state.update(_failed_update(EMPTY_PATCH, stage=STAGE_DEVELOP))
    assert state["triage"] == triage
    assert _failed_update("Triage failed.", stage=STAGE_TRIAGE)["triage"] is None
    state.update(finalize_node(state))
    assert state["failed_stage"] == STAGE_DEVELOP
    assert state["triage"] == triage

    outcome = outcome_from_state(state)

    assert outcome.status == TaskStatus.FAILED
    assert outcome.stage == STAGE_DEVELOP
    assert outcome.result_summary == EMPTY_PATCH
    assert outcome.patch is not None
    assert outcome.patch.applied.diff.decode() == second
    assert outcome.review is not None
    assert outcome.review.action is ReviewAction.REPAIR
    assert outcome.tests is not None
    assert outcome.tests.status is _TestStatus.FAILED
    assert len(outcome.repairs) == 2
    assert outcome.result_json["triage"]["summary"] == triage["summary"]


def test_accepted_review_and_an_exhausted_limit_still_finish():
    task_id = uuid4()
    kept = _diff("kept")
    accepted = outcome_from_state(
        _reviewed_state(
            task_id,
            [_cycle(task_id, 0, passed=True, action=ReviewAction.ACCEPT, diff=kept)],
            diff=kept,
            status="completed",
        )
    )
    assert accepted.status == TaskStatus.COMPLETED
    assert accepted.stage == STAGE_FINALIZE
    assert accepted.repairs == ()
    assert accepted.review is not None
    assert accepted.review.action is ReviewAction.ACCEPT

    limited_id = uuid4()
    last = _diff("three")
    limited = outcome_from_state(
        _reviewed_state(
            limited_id,
            [
                _cycle(limited_id, 0, passed=False, action=ReviewAction.REPAIR, diff=_diff("one")),
                _cycle(limited_id, 1, passed=False, action=ReviewAction.REPAIR, diff=_diff("two")),
                _cycle(limited_id, 2, passed=False, action=ReviewAction.ACCEPT, diff=last),
            ],
            diff=last,
            status="failed",
            failed_stage=STAGE_REVIEW,
            error="Tests failed and the repair limit is exhausted.",
        )
    )
    assert limited.status == TaskStatus.FAILED
    assert limited.stage == STAGE_REVIEW
    assert limited.result_summary.startswith(TEST_LIMIT_REASON)
    assert len(limited.repairs) == 2
    assert limited.patch is not None


def test_a_repair_without_a_later_failure_stays_closed():
    task_id = uuid4()
    diff = _diff("open")
    outcome = outcome_from_state(
        _reviewed_state(
            task_id,
            [
                _cycle(task_id, 0, passed=False, action=ReviewAction.REPAIR, diff=_diff("earlier")),
                _cycle(task_id, 1, passed=False, action=ReviewAction.REPAIR, diff=diff),
            ],
            diff=diff,
            status="completed",
        )
    )
    assert outcome.result_summary == REVIEW_FAILURE
    assert outcome.stage == STAGE_REVIEW
    assert outcome.patch is None


class _PromptRecorder:
    def __init__(self, proposal: PatchProposal) -> None:
        self._proposal = proposal
        self.prompts: list[str] = []

    def generate_structured(
        self,
        prompt: str,
        schema: type[object],
        options: object = None,
    ) -> PatchProposal:
        self.prompts.append(prompt)
        return self._proposal

    def close(self) -> None:
        return None


def _proposal(summary: str = "Rename the service title.") -> PatchProposal:
    return PatchProposal(
        summary=summary,
        edits=[
            FileEdit(
                relative_path="src/demo_service/app.py",
                expected_sha256="ab" * 32,
                content='app = FastAPI(title="Test service")\n',
            )
        ],
    )


def _title_plan() -> ChangePlan:
    return ChangePlan(
        problem_statement="The service title should be Test service.",
        assumptions=[],
        modifications=[
            ProposedModification(
                relative_path="src/demo_service/app.py",
                kind=ModificationKind.EXISTING,
                summary="Rename the title string.",
            )
        ],
        test_strategy="Run pytest.",
        risks=[],
        confidence=0.8,
        can_proceed=True,
    )


def _diff(marker: str) -> str:
    return (
        "diff --git a/src/demo_service/app.py b/src/demo_service/app.py\n"
        "--- a/src/demo_service/app.py\n"
        "+++ b/src/demo_service/app.py\n"
        "@@ -1 +1 @@\n"
        f"-old\n+{marker}\n"
    )


def _cycle(
    task_id: object,
    index: int,
    *,
    passed: bool,
    action: ReviewAction,
    diff: str,
) -> dict[str, object]:
    from uuid import UUID

    if not isinstance(task_id, UUID):
        raise AssertionError("task id")
    proposal = _proposal()
    review = ReviewResult(
        summary="The patch matches the plan." if passed else "The suite still returns HTTP 500.",
        action=action,
        findings=[]
        if passed
        else [
            ReviewFinding(
                summary="The suite still returns HTTP 500.",
                severity=FindingSeverity.HIGH,
                evidence="assert 500 == 422",
            )
        ],
    )
    report = _TestReport(
        job_id=_job_id(task_id, index),
        status=_TestStatus.PASSED if passed else _TestStatus.FAILED,
        exit_code=0 if passed else 1,
        timed_out=False,
        duration_ms=20,
        stdout="passed\n" if passed else "assert 500 == 422\n",
    )
    return {
        "summary": proposal.summary,
        "paths": ["src/demo_service/app.py"],
        "diff": diff,
        "proposal": proposal.model_dump(mode="json"),
        "tests": report.model_dump(mode="json"),
        "review": review.model_dump(mode="json"),
        "policy": action.value,
        "reason": "stored",
        "repair_index": index,
    }


def _reviewed_state(
    task_id: object,
    cycles: list[dict[str, object]],
    *,
    diff: str,
    status: str,
    failed_stage: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    from uuid import UUID

    if not isinstance(task_id, UUID):
        raise AssertionError("task id")
    proposal = _proposal()
    plan = _title_plan()
    triage = TriageResult(
        classification=Classification.FEATURE,
        severity=Severity.LOW,
        summary="Rename the service title.",
        acceptance_criteria=["The title string is Test service."],
        confidence=0.8,
        needs_clarification=False,
    )
    investigation = InvestigationReport(
        summary="The title is in app.py.",
        hypotheses=[],
        evidence=[],
        probable_files=["src/demo_service/app.py"],
        unanswered_questions=[],
    )
    return {
        "task_id": str(task_id),
        "title": "rename the service",
        "description": "Change the title.",
        "triage": triage.model_dump(mode="json"),
        "investigation": {
            "report": investigation.model_dump(mode="json"),
            "tool_calls": 1,
            "rejected_citations": 0,
        },
        "plan": {"plan": plan.model_dump(mode="json"), "rejected_paths": 0},
        "approval": {"decision": "approve", "approval_id": str(uuid4()), "kind": "plan"},
        "workspace": IsolatedWorkspace(
            task_id=task_id,
            base_sha="a" * 40,
            branch=branch_name(task_id),
            clone_path=clone_relative(task_id),
            worktree_path=worktree_relative(task_id),
        ).as_state(),
        "patch": {
            "summary": proposal.summary,
            "paths": ["src/demo_service/app.py"],
            "diff": diff,
            "proposal": proposal.model_dump(mode="json"),
        },
        "tests": cycles[-1]["tests"],
        "review": cycles[-1]["review"],
        "cycles": cycles,
        "publication": None,
        "status": status,
        "error": error,
        "failed_stage": failed_stage,
        "phase": failed_stage or "review",
        "repair_count": len(cycles),
    }


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
