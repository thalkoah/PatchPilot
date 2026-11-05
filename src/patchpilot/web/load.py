"""Load the rows and artifact files the dashboard shows. It does not call a model."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from patchpilot.artifacts import (
    ArtifactStoreError,
    TestReportStoreError,
    load_diff_file,
    load_plan_file,
    load_proposal_file,
    load_review_file,
    load_test_report_file,
)
from patchpilot.config import Settings
from patchpilot.db.models import Approval, Task, TaskEvent
from patchpilot.llm.plan import ChangePlan
from patchpilot.llm.review import ReviewResult
from patchpilot.repositories.approvals import ApprovalRepository
from patchpilot.repositories.artifacts import ArtifactRepository
from patchpilot.repositories.events import EventRepository
from patchpilot.repositories.model_calls import ModelCallRepository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.runner.protocol import TestReport
from patchpilot.schemas.usage import UsageLimits, UsageResponse, usage_response


@dataclass(frozen=True)
class LoadedPatch:
    """Applied diff plus the proposal checksum. The proposal body is not included."""

    version: int
    checksum: str
    proposal_checksum: str
    summary: str
    paths: tuple[str, ...]
    diff: str


@dataclass(frozen=True)
class LoadedTest:
    report: TestReport


@dataclass(frozen=True)
class LoadedTask:
    task: Task
    events: tuple[TaskEvent, ...]
    events_truncated: bool
    approvals: tuple[Approval, ...]
    plan: ChangePlan | None
    plan_version: int | None
    patch: LoadedPatch | None
    tests: LoadedTest | None
    review: ReviewResult | None
    review_policy: str | None
    repair_count: int | None
    usage: UsageResponse


async def load_task(
    *,
    tasks: TaskRepository,
    events: EventRepository,
    artifacts: ArtifactRepository,
    approvals: ApprovalRepository,
    calls: ModelCallRepository,
    settings: Settings,
    task_id: UUID,
) -> LoadedTask | None:
    """Read one task. A missing artifact leaves that section empty."""
    task = await tasks.get(task_id)
    if task is None:
        return None
    rows, truncated = await events.list_for_task(task_id)
    approval_rows = await approvals.list_for_task(task_id)
    usage_rows = await calls.list_for_task(task_id)
    limits = UsageLimits(
        model_calls=settings.task_max_model_calls,
        tool_calls=settings.task_max_tool_calls,
        tokens=settings.task_max_tokens,
        runtime_seconds=settings.task_max_runtime_seconds,
        parallelism=settings.task_max_parallelism,
        repairs=settings.task_max_repairs,
    )
    plan, plan_version = await _plan(artifacts, settings, task_id)
    patch = await _patch(artifacts, settings, task_id)
    tests = await _tests(artifacts, settings, task_id)
    review, policy, repair_count = await _review(artifacts, settings, task_id)
    return LoadedTask(
        task=task,
        events=tuple(rows),
        events_truncated=truncated,
        approvals=tuple(approval_rows),
        plan=plan,
        plan_version=plan_version,
        patch=patch,
        tests=tests,
        review=review,
        review_policy=policy,
        repair_count=repair_count,
        usage=usage_response(task_id, usage_rows, limits),
    )


async def _plan(
    artifacts: ArtifactRepository,
    settings: Settings,
    task_id: UUID,
) -> tuple[ChangePlan | None, int | None]:
    root = _root(settings)
    if root is None:
        return None, None
    row = await artifacts.get_plan(task_id)
    if row is None:
        return None, None
    try:
        plan = load_plan_file(
            root,
            relative_path=row.relative_path,
            expected_checksum=row.checksum,
            size=row.size,
        )
    except ArtifactStoreError:
        return None, None
    return plan, row.version


async def _patch(
    artifacts: ArtifactRepository,
    settings: Settings,
    task_id: UUID,
) -> LoadedPatch | None:
    root = _root(settings)
    if root is None:
        return None
    diff_row = await artifacts.get_diff(task_id)
    if diff_row is None:
        return None
    proposal_row = await artifacts.get_proposal(task_id, diff_row.version)
    if proposal_row is None:
        return None
    try:
        diff = load_diff_file(
            root,
            relative_path=diff_row.relative_path,
            expected_checksum=diff_row.checksum,
            size=diff_row.size,
        )
        proposal = load_proposal_file(
            root,
            relative_path=proposal_row.relative_path,
            expected_checksum=proposal_row.checksum,
            size=proposal_row.size,
        )
        text = diff.decode("utf-8")
    except (ArtifactStoreError, UnicodeError):
        return None
    return LoadedPatch(
        version=diff_row.version,
        checksum=diff_row.checksum,
        proposal_checksum=proposal_row.checksum,
        summary=proposal.summary,
        paths=tuple(_diff_paths(text)),
        diff=text,
    )


async def _tests(
    artifacts: ArtifactRepository,
    settings: Settings,
    task_id: UUID,
) -> LoadedTest | None:
    root = _root(settings)
    if root is None:
        return None
    row = await artifacts.get_test_report(task_id)
    if row is None:
        return None
    try:
        report = load_test_report_file(
            root,
            relative_path=row.relative_path,
            expected_checksum=row.checksum,
            size=row.size,
        )
    except TestReportStoreError:
        return None
    if report.status.value == "invalid_request":
        return None
    return LoadedTest(report=report)


async def _review(
    artifacts: ArtifactRepository,
    settings: Settings,
    task_id: UUID,
) -> tuple[ReviewResult | None, str | None, int | None]:
    root = _root(settings)
    if root is None:
        return None, None, None
    row = await artifacts.get_review(task_id)
    if row is None:
        return None, None, None
    try:
        review, policy, repair_count = load_review_file(
            root,
            relative_path=row.relative_path,
            expected_checksum=row.checksum,
            size=row.size,
        )
    except ArtifactStoreError:
        return None, None, None
    return review, policy, repair_count


def _root(settings: Settings) -> Path | None:
    if settings.artifacts_root is None:
        return None
    return Path(settings.artifacts_root)


def _diff_paths(diff: str) -> list[str]:
    paths: list[str] = []
    marker = "diff --git a/"
    for line in diff.splitlines():
        if not line.startswith(marker) or " b/" not in line:
            continue
        left, right = line[len(marker) :].split(" b/", 1)
        if left == right and left and left not in paths:
            paths.append(left)
    return paths
