"""Turn stored task state into template values. Templates do not read the database."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

from patchpilot.agents.review_policy import failure_identities
from patchpilot.audit import bound_payload
from patchpilot.db.models import Approval, Task, TaskEvent, TaskStatus
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.plan import ChangePlan
from patchpilot.llm.review import ReviewResult
from patchpilot.llm.triage import TriageResult
from patchpilot.schemas.usage import UsageCall, UsageResponse
from patchpilot.web.load import LoadedPatch, LoadedTask, LoadedTest
from patchpilot.web.redact import redact_text

STATUS_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "waiting_approval": "Waiting for approval",
    "completed": "Completed",
    "rejected": "Rejected",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
STAGE_LABELS = {
    "triage": "Triage",
    "investigate": "Investigate",
    "plan": "Plan",
    "request_plan_approval": "Approval",
    "prepare_workspace": "Workspace",
    "develop": "Develop",
    "test": "Test",
    "review": "Review",
    "request_publish_approval": "Publication approval",
    "publish": "Publication",
    "finalize": "Finalize",
    "supervisor": "Supervisor",
}
EVENT_LABELS = {
    "stage_started": "Stage started",
    "stage_completed": "Stage completed",
    "model_error": "Model error",
    "task_completed": "Task completed",
    "task_rejected": "Task rejected",
    "approval_requested": "Approval requested",
    "approval_decided": "Approval decided",
    "retry": "Retry",
    "recovery": "Recovery",
    "repair": "Repair",
    "route": "Route",
    "publication_opened": "Draft pull request opened",
}
_KIND_LABELS = {"existing": "Existing file", "new_test": "New test"}


@dataclass(frozen=True)
class EventView:
    id: str
    iso: str
    display_time: str
    stage_label: str
    event_label: str
    details: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TaskListItem:
    id: str
    title: str
    href: str
    status: str
    status_label: str
    status_class: str
    stage_label: str
    summary: str
    created: str


@dataclass(frozen=True)
class FilterLink:
    label: str
    href: str
    current: bool


@dataclass(frozen=True)
class IndexPage:
    tasks: tuple[TaskListItem, ...]
    total: int
    filters: tuple[FilterLink, ...]
    repositories: tuple[tuple[str, str], ...]
    previous_href: str | None
    next_href: str | None


@dataclass(frozen=True)
class TriageView:
    classification: str
    severity: str
    summary: str
    criteria: tuple[str, ...]
    confidence: str
    needs_clarification: bool


@dataclass(frozen=True)
class HypothesisView:
    statement: str


@dataclass(frozen=True)
class PlanHint:
    can_proceed: bool
    confidence: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ModificationView:
    path: str
    kind_label: str
    summary: str


@dataclass(frozen=True)
class PlanView:
    version: int
    problem_statement: str
    assumptions: tuple[str, ...]
    modifications: tuple[ModificationView, ...]
    test_strategy: str
    risks: tuple[str, ...]
    confidence: str
    can_proceed: bool


@dataclass(frozen=True)
class ApprovalView:
    id: str
    status_label: str
    kind: str
    kind_label: str
    plan_version: int
    reason: str
    pending: bool


@dataclass(frozen=True)
class PublicationView:
    number: int
    url: str
    branch: str
    owner: str
    name: str


@dataclass(frozen=True)
class PatchView:
    version: int
    checksum: str
    proposal_checksum: str
    summary: str
    paths: tuple[str, ...]
    diff: str


@dataclass(frozen=True)
class TestView:
    status_label: str
    passed: bool
    exit_code: str
    duration_ms: int
    stdout: str
    stderr: str
    output_truncated: bool
    unchanged_failure: str


@dataclass(frozen=True)
class FindingView:
    summary: str
    severity: str
    evidence: str


@dataclass(frozen=True)
class ReviewView:
    summary: str
    action: str
    policy: str
    repair_count: int
    findings: tuple[FindingView, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class UsageCallView:
    stage: str
    provider: str
    model: str
    prompt_tokens: str
    output_tokens: str
    latency_ms: int
    cost: str
    outcome: str


@dataclass(frozen=True)
class UsageView:
    calls: tuple[UsageCallView, ...]
    model_calls: int
    prompt_tokens: str
    output_tokens: str
    latency_ms: int
    cost: str
    model_call_limit: int
    tool_call_limit: int
    token_limit: int
    runtime_limit: str
    repair_limit: int


@dataclass(frozen=True)
class TaskPage:
    id: str
    title: str
    description: str
    status: str
    status_label: str
    status_class: str
    stage: str
    stage_label: str
    summary: str
    created: str
    updated: str
    triage: TriageView | None
    hypotheses: tuple[HypothesisView, ...]
    citations: tuple[str, ...]
    probable_files: tuple[str, ...]
    questions: tuple[str, ...]
    plan: PlanView | None
    plan_hint: PlanHint | None
    approvals: tuple[ApprovalView, ...]
    pending: ApprovalView | None
    publication: PublicationView | None
    patch: PatchView | None
    tests: TestView | None
    review: ReviewView | None
    usage: UsageView
    events: tuple[EventView, ...]
    events_truncated: bool
    progress_url: str
    decision_url: str


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, "Unknown")


def status_class(status: str) -> str:
    if status in STATUS_LABELS:
        return "status-" + status.replace("_", "-")
    return "status-unknown"


def stage_label(stage: str | None) -> str:
    if stage is None or stage == "":
        return "Not started"
    return STAGE_LABELS.get(stage, stage.replace("_", " "))


def list_href(*, status: str | None = None, offset: int = 0) -> str:
    """Index URL. Status values come from the task enum, not from model text."""
    pairs: list[tuple[str, str]] = []
    if status:
        pairs.append(("status", status))
    if offset > 0:
        pairs.append(("offset", str(offset)))
    if not pairs:
        return "/"
    return "/?" + urlencode(pairs)


def select_new_events(events: Sequence[EventView], after: str | None) -> tuple[EventView, ...]:
    """Events strictly after a cursor. An unknown cursor returns the whole list."""
    if after is None or after == "":
        return tuple(events)
    ids = [event.id for event in events]
    try:
        index = ids.index(after)
    except ValueError:
        return tuple(events)
    return tuple(events[index + 1 :])


def citation_labels(result: object) -> tuple[str, ...]:
    """Unique citation labels. A label is kept once, evidence first."""
    report = _report(result)
    if report is None:
        return ()
    labels: list[str] = []
    for item in report.evidence:
        _add(labels, item.citation)
    for hypothesis in report.hypotheses:
        for item in hypothesis.citations:
            _add(labels, item.citation)
    return tuple(labels)


def index_page(
    tasks: Sequence[Task],
    *,
    total: int,
    limit: int,
    offset: int,
    status: TaskStatus | None,
    repositories: Sequence[tuple[str, str]],
    markers: tuple[str, ...],
) -> IndexPage:
    current = None if status is None else status.value
    items = tuple(
        TaskListItem(
            id=str(task.id),
            title=redact_text(task.title, markers),
            href=f"/tasks/{task.id}",
            status=_value(task.status),
            status_label=status_label(_value(task.status)),
            status_class=status_class(_value(task.status)),
            stage_label=stage_label(task.stage),
            summary=redact_text(task.result_summary or "", markers),
            created=_display_time(task.created_at),
        )
        for task in tasks
    )
    filters = [FilterLink("All", list_href(), current is None)]
    for value, label in STATUS_LABELS.items():
        filters.append(FilterLink(label, list_href(status=value), current == value))
    previous = list_href(status=current, offset=max(offset - limit, 0)) if offset else None
    next_offset = offset + limit
    following = list_href(status=current, offset=next_offset) if next_offset < total else None
    shown = tuple((repo_id, redact_text(name, markers)) for repo_id, name in repositories)
    return IndexPage(
        tasks=items,
        total=total,
        filters=tuple(filters),
        repositories=shown,
        previous_href=previous,
        next_href=following,
    )


def progress_state(
    task: Task,
    rows: Sequence[TaskEvent],
    markers: tuple[str, ...],
) -> tuple[str, str, str, tuple[EventView, ...]]:
    """Status, stage, summary, and the full event list. The route drops seen events."""
    status = _value(task.status)
    stage = task.stage or ""
    summary = redact_text(task.result_summary or "", markers)
    events = tuple(_event(row, markers) for row in rows)
    return status, stage, summary, events


def task_page(loaded: LoadedTask, markers: tuple[str, ...]) -> TaskPage:
    """Copy one task into display values. Filesystem paths stay out of this object."""
    task = loaded.task
    status = _value(task.status)
    stage = task.stage or ""
    events = tuple(_event(row, markers) for row in loaded.events)
    approvals = tuple(_approval(row, markers) for row in loaded.approvals)
    pending = next((item for item in approvals if item.pending), None)
    summary = redact_text(task.result_summary or "", markers)
    return TaskPage(
        id=str(task.id),
        title=redact_text(task.title, markers),
        description=redact_text(task.description, markers),
        status=status,
        status_label=status_label(status),
        status_class=status_class(status),
        stage=stage,
        stage_label=stage_label(stage or None),
        summary=summary,
        created=_display_time(task.created_at),
        updated=_display_time(task.updated_at),
        triage=_triage(task.result_json, markers),
        hypotheses=_hypotheses(task.result_json, markers),
        citations=tuple(redact_text(label, markers) for label in citation_labels(task.result_json)),
        probable_files=_files(task.result_json, markers),
        questions=_questions(task.result_json, markers),
        plan=_plan(loaded.plan, loaded.plan_version, markers),
        plan_hint=None if loaded.plan is not None else _plan_hint(task.result_json, markers),
        approvals=approvals,
        pending=pending,
        publication=_publication(task.result_json),
        patch=_patch(loaded.patch, markers),
        tests=_tests(loaded.tests, loaded.task.result_json, markers),
        review=_review(loaded.review, loaded.review_policy, loaded.repair_count, markers),
        usage=_usage(loaded.usage),
        events=events,
        events_truncated=loaded.events_truncated,
        progress_url=f"/tasks/{task.id}/progress",
        decision_url=f"/api/v1/tasks/{task.id}/decisions",
    )


def _event(row: TaskEvent, markers: tuple[str, ...]) -> EventView:
    iso, display = _times(row.timestamp)
    return EventView(
        id=str(row.id),
        iso=iso,
        display_time=display,
        stage_label=stage_label(row.stage),
        event_label=EVENT_LABELS.get(row.event_type, row.event_type),
        details=_details(row.payload, markers),
    )


def _details(payload: object, markers: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if not isinstance(payload, dict):
        return ()
    cleaned = bound_payload(payload)
    rows: list[tuple[str, str]] = []
    for key, value in cleaned.items():
        text = _detail_text(value)
        if text == "":
            continue
        rows.append((redact_text(str(key), markers), redact_text(text, markers)))
    return tuple(rows)


def _detail_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list):
        parts = [_detail_text(item) for item in value[:8]]
        return ", ".join(part for part in parts if part)
    return ""


def _approval(row: Approval, markers: tuple[str, ...]) -> ApprovalView:
    status = _value(row.status)
    reason = "" if row.reason is None else redact_text(row.reason, markers)
    kind = row.kind if row.kind in {"plan", "publish"} else "plan"
    return ApprovalView(
        id=str(row.id),
        status_label=status.replace("_", " ").capitalize(),
        kind=kind,
        kind_label="Publication" if kind == "publish" else "Plan",
        plan_version=row.plan_version,
        reason=reason,
        pending=status == "pending",
    )


def _publication(result: object) -> PublicationView | None:
    """A draft link only when the stored URL matches the recorded repository."""
    if not isinstance(result, dict):
        return None
    raw = result.get("publication")
    if (
        not isinstance(raw, dict)
        or raw.get("decision") != "approve"
        or raw.get("draft") is not True
    ):
        return None
    owner = raw.get("owner")
    name = raw.get("name")
    branch = raw.get("branch")
    url = raw.get("url")
    number = raw.get("number")
    if not all(isinstance(item, str) and item for item in (owner, name, branch, url)):
        return None
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return None
    expected = f"https://github.com/{owner}/{name}/pull/{number}"
    if url != expected or not str(branch).startswith("patchpilot/"):
        return None
    return PublicationView(
        number=number,
        url=expected,
        branch=str(branch),
        owner=str(owner),
        name=str(name),
    )


def _triage(result: object, markers: tuple[str, ...]) -> TriageView | None:
    raw = _triage_raw(result)
    if raw is None:
        return None
    try:
        checked = TriageResult.model_validate(raw)
    except ValueError:
        return None
    return TriageView(
        classification=checked.classification.value,
        severity=checked.severity.value,
        summary=redact_text(checked.summary, markers),
        criteria=tuple(redact_text(item, markers) for item in checked.acceptance_criteria),
        confidence=f"{checked.confidence:.2f}",
        needs_clarification=checked.needs_clarification,
    )


def _triage_raw(result: object) -> object | None:
    if not isinstance(result, dict):
        return None
    nested = result.get("triage")
    if isinstance(nested, dict):
        return nested
    if "classification" in result:
        return result
    return None


def _report(result: object) -> InvestigationReport | None:
    if not isinstance(result, dict):
        return None
    investigation = result.get("investigation")
    if not isinstance(investigation, dict):
        return None
    try:
        return InvestigationReport.model_validate(investigation.get("report"))
    except ValueError:
        return None


def _hypotheses(result: object, markers: tuple[str, ...]) -> tuple[HypothesisView, ...]:
    report = _report(result)
    if report is None:
        return ()
    return tuple(
        HypothesisView(statement=redact_text(item.statement, markers)) for item in report.hypotheses
    )


def _files(result: object, markers: tuple[str, ...]) -> tuple[str, ...]:
    report = _report(result)
    if report is None:
        return ()
    return tuple(redact_text(path, markers) for path in report.probable_files)


def _questions(result: object, markers: tuple[str, ...]) -> tuple[str, ...]:
    report = _report(result)
    if report is None:
        return ()
    return tuple(redact_text(item, markers) for item in report.unanswered_questions)


def _plan(
    plan: ChangePlan | None,
    version: int | None,
    markers: tuple[str, ...],
) -> PlanView | None:
    if plan is None or version is None:
        return None
    modifications = tuple(
        ModificationView(
            path=redact_text(item.relative_path, markers),
            kind_label=_KIND_LABELS.get(item.kind.value, item.kind.value),
            summary=redact_text(item.summary, markers),
        )
        for item in plan.modifications
    )
    return PlanView(
        version=version,
        problem_statement=redact_text(plan.problem_statement, markers),
        assumptions=tuple(redact_text(item, markers) for item in plan.assumptions),
        modifications=modifications,
        test_strategy=redact_text(plan.test_strategy, markers),
        risks=tuple(redact_text(item, markers) for item in plan.risks),
        confidence=f"{plan.confidence:.2f}",
        can_proceed=plan.can_proceed,
    )


def _plan_hint(result: object, markers: tuple[str, ...]) -> PlanHint | None:
    if not isinstance(result, dict):
        return None
    raw = result.get("plan")
    if not isinstance(raw, dict) or not isinstance(raw.get("can_proceed"), bool):
        return None
    paths = raw.get("paths")
    if not isinstance(paths, list):
        return None
    names = tuple(
        redact_text(item, markers) for item in paths if isinstance(item, str) and item.strip()
    )
    confidence = raw.get("confidence")
    shown = ""
    if isinstance(confidence, int | float) and not isinstance(confidence, bool):
        shown = f"{confidence:.2f}"
    return PlanHint(can_proceed=raw["can_proceed"], confidence=shown, paths=names)


def _patch(loaded: LoadedPatch | None, markers: tuple[str, ...]) -> PatchView | None:
    if loaded is None:
        return None
    return PatchView(
        version=loaded.version,
        checksum=loaded.checksum,
        proposal_checksum=loaded.proposal_checksum,
        summary=redact_text(loaded.summary, markers),
        paths=tuple(redact_text(path, markers) for path in loaded.paths),
        diff=redact_text(loaded.diff, markers),
    )


def _tests(
    loaded: LoadedTest | None,
    result: object,
    markers: tuple[str, ...],
) -> TestView | None:
    if loaded is None:
        return None
    report = loaded.report
    exit_code = "none" if report.exit_code is None else str(report.exit_code)
    return TestView(
        status_label=report.status.value.replace("_", " "),
        passed=report.status.value == "passed",
        exit_code=exit_code,
        duration_ms=report.duration_ms,
        stdout=redact_text(report.stdout, markers),
        stderr=redact_text(report.stderr, markers),
        output_truncated=report.output_truncated,
        unchanged_failure=redact_text(_unchanged_failure(report.stdout, result), markers),
    )


def _unchanged_failure(stdout: str, result: object) -> str:
    """Name baseline identities when the patched log repeats them and nothing else."""
    if not isinstance(result, dict):
        return ""
    raw = result.get("baseline_failures")
    if not isinstance(raw, list):
        return ""
    known = {item for item in raw if isinstance(item, str) and item.strip()}
    current = failure_identities(stdout)
    if not current or not known or any(item not in known for item in current):
        return ""
    shown = ", ".join(current)
    return f"This failure was already on the unmodified worktree: {shown}."


def _review(
    review: ReviewResult | None,
    policy: str | None,
    repair_count: int | None,
    markers: tuple[str, ...],
) -> ReviewView | None:
    if review is None or policy is None or repair_count is None:
        return None
    findings = tuple(
        FindingView(
            summary=redact_text(item.summary, markers),
            severity=item.severity.value,
            evidence=redact_text(item.evidence, markers),
        )
        for item in review.findings
    )
    return ReviewView(
        summary=redact_text(review.summary, markers),
        action=review.action.value,
        policy=policy,
        repair_count=repair_count,
        findings=findings,
        evidence=tuple(redact_text(item, markers) for item in review.evidence),
    )


def _usage(usage: UsageResponse) -> UsageView:
    totals = usage.totals
    cost = "unknown"
    if totals.cost == "estimated" and totals.estimated_cost_usd is not None:
        cost = totals.estimated_cost_usd
    return UsageView(
        calls=tuple(_usage_call(item) for item in usage.calls),
        model_calls=totals.model_calls,
        prompt_tokens=_tokens(totals.prompt_tokens),
        output_tokens=_tokens(totals.output_tokens),
        latency_ms=totals.latency_ms,
        cost=cost,
        model_call_limit=usage.limits.model_calls,
        tool_call_limit=usage.limits.tool_calls,
        token_limit=usage.limits.tokens,
        runtime_limit=str(usage.limits.runtime_seconds),
        repair_limit=usage.limits.repairs,
    )


def _usage_call(item: UsageCall) -> UsageCallView:
    cost = "unknown"
    if item.cost == "estimated" and item.estimated_cost_usd is not None:
        cost = item.estimated_cost_usd
    return UsageCallView(
        stage=item.stage,
        provider=item.provider,
        model=item.model,
        prompt_tokens=_tokens(item.prompt_tokens),
        output_tokens=_tokens(item.output_tokens),
        latency_ms=item.latency_ms,
        cost=cost,
        outcome=item.outcome,
    )


def _tokens(value: int | None) -> str:
    if value is None:
        return "not reported"
    return str(value)


def _add(labels: list[str], label: str) -> None:
    if label and label not in labels:
        labels.append(label)


def _value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _display_time(value: datetime) -> str:
    return _times(value)[1]


def _times(value: datetime) -> tuple[str, str]:
    utc = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    iso = utc.isoformat(timespec="seconds")
    return iso, utc.strftime("%Y-%m-%d %H:%M:%S UTC")
