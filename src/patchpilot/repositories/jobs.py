"""Claim, lease, and finish a task run.

Recovery policy
---------------
A claim sets ``lease_until`` from the database clock plus ``lease_seconds`` and
commits before any model call. The owning worker renews that lease on a separate
connection, and only while ``lease_until`` is still in the future, the run is
open, and ``worker_id`` still matches. A renewal that matches no row means the
lease was lost. The worker then discards its in-memory result.

A running task whose open run has ``lease_until`` in the past is recoverable.
The next worker locks that task with ``FOR UPDATE SKIP LOCKED``. When the open
attempt is below ``max_attempts``, it closes the run with ``lease_expired``,
records ``recovery`` and ``retry`` events, and starts the next attempt. That
attempt resumes the LangGraph thread when a checkpoint exists, and otherwise
replays triage. When the open attempt is already the last allowed attempt, the
task is stored as ``failed`` with ``recovery_exhausted`` and is not claimed again.

A plan that names a code change does not finish the run. The worker stores a
pending approval, sets ``waiting_approval``, and ends the lease. A later claim
resumes only after the decision row is stored. Events are append-only.
Repeating a write with the same idempotency key does not change the stored
payload.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.agents.developer import DevelopOutcome
from patchpilot.agents.reviewer import ReviewedCycle
from patchpilot.agents.supervisor import route_records
from patchpilot.artifacts import (
    ArtifactStoreError,
    TestReportStoreError,
    checksum,
    test_report_document,
)
from patchpilot.audit import (
    APPROVAL_REQUESTED,
    ERROR_LEASE_EXPIRED,
    ERROR_MODEL,
    ERROR_RECOVERY_EXHAUSTED,
    MODEL_ERROR,
    PUBLICATION_OPENED,
    RECOVERY,
    RECOVERY_EXHAUSTED,
    RECOVERY_RESUME,
    REPAIR,
    RETRY,
    ROUTE,
    STAGE_COMPLETED,
    STAGE_STARTED,
    TASK_COMPLETED,
    TASK_REJECTED,
    approval_requested_key,
    event_key,
    publication_opened_key,
    repair_event_key,
    require_attempts,
    require_lease,
    require_stage,
    require_worker_id,
    route_event_key,
    task_completed_key,
    task_rejected_key,
)
from patchpilot.db.models import (
    APPROVAL_KIND_PLAN,
    APPROVAL_KIND_PUBLISH,
    Task,
    TaskEvent,
    TaskRun,
    TaskStatus,
)
from patchpilot.llm.plan import ChangePlan
from patchpilot.repositories.approvals import insert_pending_approval
from patchpilot.repositories.artifacts import (
    persist_patch,
    persist_plan,
    persist_review,
    persist_test_report,
)
from patchpilot.repositories.events import append_event
from patchpilot.repositories.publications import PublicationStoreError, insert_publication
from patchpilot.repositories.workspaces import insert_workspace
from patchpilot.runner.protocol import TestReport
from patchpilot.workspace.errors import WorkspaceStoreError
from patchpilot.workspace.layout import IsolatedWorkspace

# Stage names written when the graph finishes. Investigate is present only when
# the task named a repository and the report was stored.
_STAGE_TRIAGE = "triage"
_STAGE_INVESTIGATE = "investigate"
_STAGE_PLAN = "plan"
_STAGE_APPROVAL = "request_plan_approval"
_STAGE_PUBLISH_APPROVAL = "request_publish_approval"
_STAGE_PUBLISH = "publish"
_STAGE_WORKSPACE = "prepare_workspace"
_STAGE_DEVELOP = "develop"
_STAGE_TEST = "test"
_STAGE_REVIEW = "review"
_STAGE_FINALIZE = "finalize"
_STAGE_SUPERVISOR = "supervisor"


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    """A run this worker owns. The claim transaction is already committed."""

    id: UUID
    title: str
    description: str
    run_id: UUID
    attempt: int
    worker_id: str
    repo_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One poll result. ``acted`` means the queue was not empty."""

    claimed: ClaimedTask | None
    acted: bool
    recovered: bool = False
    exhausted_task_id: UUID | None = None


class JobRepository:
    """Worker persistence. Mutating methods commit so the row survives the session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def take_next(
        self,
        *,
        stage: str,
        worker_id: str,
        lease_seconds: float,
        max_attempts: int,
    ) -> WorkItem:
        """Recover an expired run, or claim the oldest queued task."""
        recovered = await self.recover_expired(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        if recovered is not None:
            return recovered
        claimed = await self.claim_next_queued(
            stage=stage,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            return WorkItem(claimed=None, acted=False)
        return WorkItem(claimed=claimed, acted=True)

    async def claim_next_queued(
        self,
        *,
        stage: str,
        worker_id: str,
        lease_seconds: float,
    ) -> ClaimedTask | None:
        """Claim the oldest queued task and commit before the caller returns."""
        statement = (
            select(Task)
            .where(Task.status == TaskStatus.QUEUED)
            .order_by(Task.created_at.asc(), Task.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return await self._claim(
            statement,
            stage=stage,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    async def claim_queued(
        self,
        task_id: UUID,
        *,
        stage: str,
        worker_id: str,
        lease_seconds: float,
    ) -> ClaimedTask | None:
        """Claim one queued task by id. Return None when it is no longer queued."""
        statement = (
            select(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return await self._claim(
            statement,
            stage=stage,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    async def recover_expired(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        max_attempts: int,
    ) -> WorkItem | None:
        """Take over one crashed run, or fail it when no attempts remain.

        Return None when every open lease is still valid. The caller may then
        claim a queued task.
        """
        checked_worker = require_worker_id(worker_id)
        checked_lease = require_lease(lease_seconds)
        checked_attempts = require_attempts(max_attempts)
        statement = (
            select(Task, TaskRun)
            .join(TaskRun, TaskRun.task_id == Task.id)
            .where(
                Task.status == TaskStatus.RUNNING,
                TaskRun.ended_at.is_(None),
                TaskRun.lease_until.is_not(None),
                TaskRun.lease_until < func.now(),
            )
            .order_by(TaskRun.lease_until.asc(), Task.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True, of=Task)
        )
        row = (await self._session.execute(statement)).first()
        if row is None:
            await self._session.rollback()
            return None
        task, run = row
        stage = task.stage or _STAGE_TRIAGE
        if run.attempt >= checked_attempts:
            await self._exhaust(task, run, stage=stage)
            await self._session.commit()
            return WorkItem(claimed=None, acted=True, exhausted_task_id=task.id)
        run.ended_at = datetime.now(UTC)
        run.error_code = ERROR_LEASE_EXPIRED
        await self._session.flush()
        await append_event(
            self._session,
            task_id=task.id,
            run_id=run.id,
            stage=stage,
            event_type=RECOVERY,
            payload={"attempt": run.attempt, "action": RECOVERY_RESUME},
            idempotency_key=event_key(
                task_id=task.id,
                run_id=run.id,
                event_type=RECOVERY,
                stage=stage,
            ),
            ordinal=0,
        )
        attempt = run.attempt + 1
        run_id = uuid4()
        await self._insert_run(
            run_id=run_id,
            task_id=task.id,
            attempt=attempt,
            worker_id=checked_worker,
            lease_seconds=checked_lease,
        )
        await append_event(
            self._session,
            task_id=task.id,
            run_id=run_id,
            stage=stage,
            event_type=RETRY,
            payload={"attempt": attempt, "previous_attempt": run.attempt},
            idempotency_key=event_key(
                task_id=task.id,
                run_id=run_id,
                event_type=RETRY,
                stage=stage,
            ),
            ordinal=1,
        )
        await append_event(
            self._session,
            task_id=task.id,
            run_id=run_id,
            stage=stage,
            event_type=STAGE_STARTED,
            payload={"attempt": attempt},
            idempotency_key=event_key(
                task_id=task.id,
                run_id=run_id,
                event_type=STAGE_STARTED,
                stage=stage,
            ),
            ordinal=2,
        )
        claimed = ClaimedTask(
            id=task.id,
            title=task.title,
            description=task.description,
            run_id=run_id,
            attempt=attempt,
            worker_id=checked_worker,
            repo_id=task.repo_id,
        )
        await self._session.commit()
        return WorkItem(claimed=claimed, acted=True, recovered=True)

    async def renew_lease(self, claimed: ClaimedTask, *, lease_seconds: float) -> bool:
        """Extend a lease this worker still holds. An expired lease stays expired."""
        checked_lease = require_lease(lease_seconds)
        result = await self._session.execute(
            update(TaskRun)
            .where(
                TaskRun.id == claimed.run_id,
                TaskRun.worker_id == claimed.worker_id,
                TaskRun.ended_at.is_(None),
                TaskRun.lease_until > func.now(),
            )
            .values(lease_until=_lease_deadline(checked_lease))
            .returning(TaskRun.id)
        )
        renewed = result.scalar_one_or_none() is not None
        if renewed:
            await self._session.commit()
        else:
            await self._session.rollback()
        return renewed

    async def finish_run(
        self,
        claimed: ClaimedTask,
        *,
        status: TaskStatus,
        stage: str,
        result_summary: str,
        result_json: dict[str, Any],
        plan: ChangePlan | None = None,
        workspace: IsolatedWorkspace | None = None,
        patch: DevelopOutcome | None = None,
        tests: TestReport | None = None,
        cycles: tuple[ReviewedCycle, ...] = (),
        artifacts_root: Path | None = None,
        routes: tuple[dict[str, Any], ...] = (),
    ) -> bool:
        """Store a terminal outcome when this worker still holds the lease.

        Return False when another worker has taken the run. The caller must not
        treat that as a failure of the task. A completed or rejected plan is
        written to the artifact volume before this commit. `ArtifactStoreError`
        means nothing was committed.
        """
        if status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.REJECTED}:
            raise ValueError("a finished run must be completed, rejected, or failed")
        checked_stage = require_stage(stage)
        if not result_summary.strip():
            raise ValueError("result_summary must not be blank")
        task = await self._owned_running_task(claimed)
        if task is None:
            await self._session.rollback()
            return False
        stored = dict(result_json)
        if plan is not None and (
            status in {TaskStatus.COMPLETED, TaskStatus.REJECTED} or bool(cycles)
        ):
            stored = await self._with_plan_version(
                claimed.id,
                stored,
                plan,
                artifacts_root,
            )
        if cycles:
            if status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                raise ArtifactStoreError
            stored = await self._with_cycles(claimed.id, stored, cycles, artifacts_root)
        elif patch is not None:
            if status != TaskStatus.COMPLETED or workspace is None:
                raise ArtifactStoreError
            stored = await self._with_patch_version(
                claimed.id,
                stored,
                patch,
                artifacts_root,
            )
        if tests is not None:
            if cycles or status != TaskStatus.COMPLETED or patch is None:
                raise TestReportStoreError
            stored = await self._with_test_version(
                claimed.id,
                stored,
                tests,
                artifacts_root,
            )
        elif patch is not None:
            raise TestReportStoreError
        if workspace is not None:
            if status != TaskStatus.COMPLETED or claimed.repo_id is None:
                raise WorkspaceStoreError
            await insert_workspace(
                self._session,
                task_id=claimed.id,
                repo_id=claimed.repo_id,
                workspace=workspace,
            )
        if status == TaskStatus.COMPLETED:
            await self._store_publication(claimed.id, stored)
        task.status = status
        task.stage = checked_stage
        task.result_summary = result_summary
        task.result_json = stored
        await self._session.execute(
            update(TaskRun)
            .where(TaskRun.id == claimed.run_id)
            .values(
                ended_at=func.now(),
                error_code=ERROR_MODEL if status == TaskStatus.FAILED else None,
            )
        )
        if status == TaskStatus.FAILED:
            if cycles:
                await self._record_reviewed_failure(
                    task.id,
                    claimed,
                    stored,
                    stage=checked_stage,
                    result_summary=result_summary,
                    routes=routes,
                )
            else:
                await self._record_model_error(
                    task.id,
                    claimed,
                    stage=checked_stage,
                    result_summary=result_summary,
                    routes=routes,
                )
        else:
            await self._record_success(
                task.id,
                claimed,
                stored,
                terminal=status,
                workspace=workspace if status == TaskStatus.COMPLETED else None,
                routes=routes,
            )
        await self._session.commit()
        return True

    async def pause_for_approval(
        self,
        claimed: ClaimedTask,
        *,
        stage: str,
        result_summary: str,
        result_json: dict[str, Any],
        plan: ChangePlan,
        artifacts_root: Path | None,
        routes: tuple[dict[str, Any], ...] = (),
        kind: str = APPROVAL_KIND_PLAN,
        patch: DevelopOutcome | None = None,
        tests: TestReport | None = None,
        cycles: tuple[ReviewedCycle, ...] = (),
        workspace: IsolatedWorkspace | None = None,
    ) -> bool:
        """Store the plan, open one approval, and release the lease.

        Return False when this worker no longer holds the run. The checkout is
        not written. A second pause for the same task reuses the pending row.
        A publication pause also stores the patch, tests, and review that the
        human is being asked to publish.
        """
        checked_stage = require_stage(stage)
        if kind not in {APPROVAL_KIND_PLAN, APPROVAL_KIND_PUBLISH}:
            raise ValueError("approval kind must be plan or publish")
        if not result_summary.strip():
            raise ValueError("result_summary must not be blank")
        task = await self._owned_running_task(claimed)
        if task is None:
            await self._session.rollback()
            return False
        stored = await self._with_plan_version(
            claimed.id,
            dict(result_json),
            plan,
            artifacts_root,
        )
        if kind == APPROVAL_KIND_PUBLISH:
            stored = await self._store_review_artifacts(
                claimed,
                stored,
                patch=patch,
                tests=tests,
                cycles=cycles,
                workspace=workspace,
                artifacts_root=artifacts_root,
            )
        version = stored.get("plan", {}).get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ArtifactStoreError
        approval = await insert_pending_approval(
            self._session,
            task_id=task.id,
            plan_version=version,
            kind=kind,
        )
        task.status = TaskStatus.WAITING_APPROVAL
        task.stage = checked_stage
        task.result_summary = result_summary
        task.result_json = stored
        await self._session.execute(
            update(TaskRun)
            .where(TaskRun.id == claimed.run_id)
            .values(ended_at=func.now(), error_code=None)
        )
        await self._record_success(
            task.id,
            claimed,
            stored,
            waiting=True,
            approval_id=approval.id,
            approval_kind=kind,
            routes=routes,
        )
        await self._session.commit()
        return True

    async def _claim(
        self,
        statement: object,
        *,
        stage: str,
        worker_id: str,
        lease_seconds: float,
    ) -> ClaimedTask | None:
        checked_stage = require_stage(stage)
        checked_worker = require_worker_id(worker_id)
        checked_lease = require_lease(lease_seconds)
        task = await self._session.scalar(statement)
        if task is None:
            await self._session.rollback()
            return None
        attempt = await self._next_attempt(task.id)
        task.status = TaskStatus.RUNNING
        task.stage = checked_stage
        run_id = uuid4()
        await self._insert_run(
            run_id=run_id,
            task_id=task.id,
            attempt=attempt,
            worker_id=checked_worker,
            lease_seconds=checked_lease,
        )
        await append_event(
            self._session,
            task_id=task.id,
            run_id=run_id,
            stage=checked_stage,
            event_type=STAGE_STARTED,
            payload={"attempt": attempt},
            idempotency_key=event_key(
                task_id=task.id,
                run_id=run_id,
                event_type=STAGE_STARTED,
                stage=checked_stage,
            ),
            ordinal=0,
        )
        claimed = ClaimedTask(
            id=task.id,
            title=task.title,
            description=task.description,
            run_id=run_id,
            attempt=attempt,
            worker_id=checked_worker,
            repo_id=task.repo_id,
        )
        await self._session.commit()
        return claimed

    async def _exhaust(self, task: Task, run: TaskRun, *, stage: str) -> None:
        summary = f"Recovery stopped after attempt {run.attempt}."
        task.status = TaskStatus.FAILED
        task.stage = stage
        task.result_summary = summary
        task.result_json = {"error": summary, "error_code": ERROR_RECOVERY_EXHAUSTED}
        run.ended_at = datetime.now(UTC)
        run.error_code = ERROR_RECOVERY_EXHAUSTED
        await append_event(
            self._session,
            task_id=task.id,
            run_id=run.id,
            stage=stage,
            event_type=RECOVERY,
            payload={"attempt": run.attempt, "action": RECOVERY_EXHAUSTED},
            idempotency_key=event_key(
                task_id=task.id,
                run_id=run.id,
                event_type=RECOVERY,
                stage=stage,
            ),
            ordinal=0,
        )

    async def _owned_running_task(self, claimed: ClaimedTask) -> Task | None:
        task = await self._session.scalar(
            select(Task).where(Task.id == claimed.id).with_for_update()
        )
        if task is None or task.status != TaskStatus.RUNNING:
            return None
        owned = await self._session.scalar(
            select(TaskRun.id).where(
                TaskRun.id == claimed.run_id,
                TaskRun.worker_id == claimed.worker_id,
                TaskRun.ended_at.is_(None),
                TaskRun.lease_until > func.now(),
            )
        )
        if owned is None:
            return None
        return task

    async def _store_review_artifacts(
        self,
        claimed: ClaimedTask,
        stored: dict[str, Any],
        *,
        patch: DevelopOutcome | None,
        tests: TestReport | None,
        cycles: tuple[ReviewedCycle, ...],
        workspace: IsolatedWorkspace | None,
        artifacts_root: Path | None,
    ) -> dict[str, Any]:
        """Persist the reviewed patch before the publication pause. The caller commits."""
        if cycles:
            stored = await self._with_cycles(claimed.id, stored, cycles, artifacts_root)
        elif patch is not None:
            stored = await self._with_patch_version(claimed.id, stored, patch, artifacts_root)
            if tests is None:
                raise TestReportStoreError
            stored = await self._with_test_version(claimed.id, stored, tests, artifacts_root)
        if workspace is not None:
            if claimed.repo_id is None:
                raise WorkspaceStoreError
            await insert_workspace(
                self._session,
                task_id=claimed.id,
                repo_id=claimed.repo_id,
                workspace=workspace,
            )
        return stored

    async def _store_publication(self, task_id: UUID, stored: dict[str, Any]) -> None:
        """Record a draft that the graph already opened. A rejection stores nothing."""
        publication = stored.get("publication")
        if not isinstance(publication, dict) or publication.get("decision") != "approve":
            return
        try:
            approval_id = UUID(str(publication.get("approval_id")))
            number = publication.get("number")
            if isinstance(number, bool) or not isinstance(number, int):
                raise PublicationStoreError
            await insert_publication(
                self._session,
                task_id=task_id,
                approval_id=approval_id,
                owner=str(publication.get("owner")),
                name=str(publication.get("name")),
                branch=str(publication.get("branch")),
                base_branch=str(publication.get("base")),
                head_sha=str(publication.get("head_sha")),
                pr_number=number,
                html_url=str(publication.get("url")),
            )
        except (PublicationStoreError, ValueError):
            raise PublicationStoreError from None

    async def _with_plan_version(
        self,
        task_id: UUID,
        stored: dict[str, Any],
        plan: ChangePlan,
        artifacts_root: Path | None,
    ) -> dict[str, Any]:
        if artifacts_root is None:
            raise ArtifactStoreError
        version = await persist_plan(self._session, artifacts_root, task_id, plan)
        meta = stored.get("plan")
        plan_meta = dict(meta) if isinstance(meta, dict) else {}
        plan_meta["version"] = version
        stored["plan"] = plan_meta
        return stored

    async def _with_patch_version(
        self,
        task_id: UUID,
        stored: dict[str, Any],
        patch: DevelopOutcome,
        artifacts_root: Path | None,
    ) -> dict[str, Any]:
        if artifacts_root is None:
            raise ArtifactStoreError
        version = await persist_patch(
            self._session,
            artifacts_root,
            task_id,
            patch.proposal,
            patch.applied.diff,
        )
        meta = stored.get("patch")
        patch_meta = dict(meta) if isinstance(meta, dict) else {}
        patch_meta["version"] = version
        stored["patch"] = patch_meta
        return stored

    async def _with_test_version(
        self,
        task_id: UUID,
        stored: dict[str, Any],
        report: TestReport,
        artifacts_root: Path | None,
    ) -> dict[str, Any]:
        if artifacts_root is None:
            raise TestReportStoreError
        version = await persist_test_report(self._session, artifacts_root, task_id, report)
        meta = stored.get("tests")
        tests_meta = dict(meta) if isinstance(meta, dict) else {}
        tests_meta["version"] = version
        tests_meta["checksum"] = checksum(test_report_document(report))
        stored["tests"] = tests_meta
        return stored

    async def _record_model_error(
        self,
        task_id: UUID,
        claimed: ClaimedTask,
        *,
        stage: str,
        result_summary: str,
        routes: tuple[dict[str, Any], ...] = (),
    ) -> None:
        pending = await self._unwritten_routes(task_id, routes)
        for offset, item in enumerate(pending):
            await append_event(
                self._session,
                task_id=task_id,
                run_id=claimed.run_id,
                stage=_STAGE_SUPERVISOR,
                event_type=ROUTE,
                payload=_route_payload(item, claimed.attempt),
                idempotency_key=item["key"],
                ordinal=offset,
            )
        await append_event(
            self._session,
            task_id=task_id,
            run_id=claimed.run_id,
            stage=stage,
            event_type=MODEL_ERROR,
            payload={
                "attempt": claimed.attempt,
                "error_code": ERROR_MODEL,
                "message": result_summary,
            },
            idempotency_key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=MODEL_ERROR,
                stage=stage,
            ),
            ordinal=len(pending),
        )

    async def _record_success(
        self,
        task_id: UUID,
        claimed: ClaimedTask,
        result_json: dict[str, Any],
        *,
        waiting: bool = False,
        approval_id: UUID | None = None,
        approval_kind: str = APPROVAL_KIND_PLAN,
        terminal: TaskStatus = TaskStatus.COMPLETED,
        workspace: IsolatedWorkspace | None = None,
        routes: tuple[dict[str, Any], ...] = (),
    ) -> None:
        attempt = {"attempt": claimed.attempt}
        ordinal = 0

        async def emit(
            *,
            stage: str,
            event_type: str,
            payload: dict[str, Any],
            key: str,
            run_id: UUID | None,
        ) -> None:
            nonlocal ordinal
            await append_event(
                self._session,
                task_id=task_id,
                run_id=run_id,
                stage=stage,
                event_type=event_type,
                payload=payload,
                idempotency_key=key,
                ordinal=ordinal,
            )
            ordinal += 1

        if not await self._stage_completed(task_id, _STAGE_TRIAGE):
            await emit(
                stage=_STAGE_TRIAGE,
                event_type=STAGE_COMPLETED,
                payload=attempt,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_TRIAGE,
                ),
                run_id=claimed.run_id,
            )
        await self._emit_routes(task_id, claimed, routes, emit)
        investigation = _investigation_payload(result_json, claimed.attempt)
        if investigation is not None and not await self._stage_completed(
            task_id, _STAGE_INVESTIGATE
        ):
            await emit(
                stage=_STAGE_INVESTIGATE,
                event_type=STAGE_STARTED,
                payload=attempt,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_INVESTIGATE,
                ),
                run_id=claimed.run_id,
            )
            await emit(
                stage=_STAGE_INVESTIGATE,
                event_type=STAGE_COMPLETED,
                payload=investigation,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_INVESTIGATE,
                ),
                run_id=claimed.run_id,
            )
        plan = _plan_payload(result_json, claimed.attempt)
        if plan is not None and not await self._stage_completed(task_id, _STAGE_PLAN):
            await emit(
                stage=_STAGE_PLAN,
                event_type=STAGE_STARTED,
                payload=attempt,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_PLAN,
                ),
                run_id=claimed.run_id,
            )
            await emit(
                stage=_STAGE_PLAN,
                event_type=STAGE_COMPLETED,
                payload=plan,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_PLAN,
                ),
                run_id=claimed.run_id,
            )
        if waiting:
            paths = plan.get("paths") if isinstance(plan, dict) else []
            pause_stage = (
                _STAGE_PUBLISH_APPROVAL
                if approval_kind == APPROVAL_KIND_PUBLISH
                else _STAGE_APPROVAL
            )
            await emit(
                stage=pause_stage,
                event_type=APPROVAL_REQUESTED,
                payload={
                    "attempt": claimed.attempt,
                    "plan_version": None if plan is None else plan.get("version"),
                    "approval_id": None if approval_id is None else str(approval_id),
                    "kind": approval_kind,
                    "paths": paths if isinstance(paths, list) else [],
                },
                key=approval_requested_key(task_id, approval_kind),
                run_id=claimed.run_id,
            )
            return
        if workspace is not None and not await self._stage_completed(task_id, _STAGE_WORKSPACE):
            workspace_payload = {
                "attempt": claimed.attempt,
                "base_sha": workspace.base_sha,
                "branch": workspace.branch,
            }
            await emit(
                stage=_STAGE_WORKSPACE,
                event_type=STAGE_STARTED,
                payload=attempt,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_WORKSPACE,
                ),
                run_id=claimed.run_id,
            )
            await emit(
                stage=_STAGE_WORKSPACE,
                event_type=STAGE_COMPLETED,
                payload=workspace_payload,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_WORKSPACE,
                ),
                run_id=claimed.run_id,
            )
        patch = _patch_payload(result_json, claimed.attempt)
        if patch is not None and not await self._stage_completed(task_id, _STAGE_DEVELOP):
            await emit(
                stage=_STAGE_DEVELOP,
                event_type=STAGE_STARTED,
                payload=attempt,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_DEVELOP,
                ),
                run_id=claimed.run_id,
            )
            await emit(
                stage=_STAGE_DEVELOP,
                event_type=STAGE_COMPLETED,
                payload=patch,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_DEVELOP,
                ),
                run_id=claimed.run_id,
            )
        tests = _tests_payload(result_json, claimed.attempt)
        if tests is not None and not await self._stage_completed(task_id, _STAGE_TEST):
            await emit(
                stage=_STAGE_TEST,
                event_type=STAGE_STARTED,
                payload=attempt,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_TEST,
                ),
                run_id=claimed.run_id,
            )
            await emit(
                stage=_STAGE_TEST,
                event_type=STAGE_COMPLETED,
                payload=tests,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_TEST,
                ),
                run_id=claimed.run_id,
            )
        await self._emit_review_trail(task_id, claimed, result_json, emit)
        await emit(
            stage=_STAGE_FINALIZE,
            event_type=STAGE_STARTED,
            payload=attempt,
            key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=STAGE_STARTED,
                stage=_STAGE_FINALIZE,
            ),
            run_id=claimed.run_id,
        )
        await emit(
            stage=_STAGE_FINALIZE,
            event_type=STAGE_COMPLETED,
            payload=attempt,
            key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=STAGE_COMPLETED,
                stage=_STAGE_FINALIZE,
            ),
            run_id=claimed.run_id,
        )
        if terminal == TaskStatus.REJECTED:
            await emit(
                stage=_STAGE_FINALIZE,
                event_type=TASK_REJECTED,
                payload={**attempt, "status": TaskStatus.REJECTED.value},
                key=task_rejected_key(task_id),
                run_id=None,
            )
            return
        opened = _opened_publication(result_json)
        if opened is not None:
            await emit(
                stage=_STAGE_PUBLISH,
                event_type=PUBLICATION_OPENED,
                payload=opened,
                key=publication_opened_key(task_id),
                run_id=None,
            )
        await emit(
            stage=_STAGE_FINALIZE,
            event_type=TASK_COMPLETED,
            payload={**attempt, "status": TaskStatus.COMPLETED.value},
            key=task_completed_key(task_id),
            run_id=None,
        )

    async def _record_reviewed_failure(
        self,
        task_id: UUID,
        claimed: ClaimedTask,
        result_json: dict[str, Any],
        *,
        stage: str,
        result_summary: str,
        routes: tuple[dict[str, Any], ...] = (),
    ) -> None:
        """Record the patch, the tests, each repair reason, and the review failure."""
        attempt = {"attempt": claimed.attempt}
        ordinal = 0

        async def emit(
            *,
            stage: str,
            event_type: str,
            payload: dict[str, Any],
            key: str,
            run_id: UUID | None,
        ) -> None:
            nonlocal ordinal
            await append_event(
                self._session,
                task_id=task_id,
                run_id=run_id,
                stage=stage,
                event_type=event_type,
                payload=payload,
                idempotency_key=key,
                ordinal=ordinal,
            )
            ordinal += 1

        await self._emit_routes(task_id, claimed, routes, emit)
        patch = _patch_payload(result_json, claimed.attempt)
        if patch is not None and not await self._stage_completed(task_id, _STAGE_DEVELOP):
            await emit(
                stage=_STAGE_DEVELOP,
                event_type=STAGE_STARTED,
                payload=attempt,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_DEVELOP,
                ),
                run_id=claimed.run_id,
            )
            await emit(
                stage=_STAGE_DEVELOP,
                event_type=STAGE_COMPLETED,
                payload=patch,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_DEVELOP,
                ),
                run_id=claimed.run_id,
            )
        tests = _tests_payload(result_json, claimed.attempt)
        if tests is not None and not await self._stage_completed(task_id, _STAGE_TEST):
            await emit(
                stage=_STAGE_TEST,
                event_type=STAGE_STARTED,
                payload=attempt,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_TEST,
                ),
                run_id=claimed.run_id,
            )
            await emit(
                stage=_STAGE_TEST,
                event_type=STAGE_COMPLETED,
                payload=tests,
                key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_TEST,
                ),
                run_id=claimed.run_id,
            )
        await self._emit_review_trail(task_id, claimed, result_json, emit)
        await emit(
            stage=stage,
            event_type=MODEL_ERROR,
            payload={
                "attempt": claimed.attempt,
                "error_code": ERROR_MODEL,
                "message": result_summary,
            },
            key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=MODEL_ERROR,
                stage=stage,
            ),
            run_id=claimed.run_id,
        )

    async def _emit_review_trail(
        self,
        task_id: UUID,
        claimed: ClaimedTask,
        result_json: dict[str, Any],
        emit: Any,
    ) -> None:
        attempt = {"attempt": claimed.attempt}
        for repair in _repair_payloads(result_json, claimed.attempt):
            index = repair["index"]
            await emit(
                stage=_STAGE_REVIEW,
                event_type=REPAIR,
                payload=repair,
                key=repair_event_key(task_id, index),
                run_id=claimed.run_id,
            )
        review = _review_payload(result_json, claimed.attempt)
        if review is None or await self._stage_completed(task_id, _STAGE_REVIEW):
            return
        await emit(
            stage=_STAGE_REVIEW,
            event_type=STAGE_STARTED,
            payload=attempt,
            key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=STAGE_STARTED,
                stage=_STAGE_REVIEW,
            ),
            run_id=claimed.run_id,
        )
        await emit(
            stage=_STAGE_REVIEW,
            event_type=STAGE_COMPLETED,
            payload=review,
            key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=STAGE_COMPLETED,
                stage=_STAGE_REVIEW,
            ),
            run_id=claimed.run_id,
        )

    async def _with_cycles(
        self,
        task_id: UUID,
        stored: dict[str, Any],
        cycles: tuple[ReviewedCycle, ...],
        artifacts_root: Path | None,
    ) -> dict[str, Any]:
        if artifacts_root is None or not cycles:
            raise ArtifactStoreError
        patch_version = 0
        test_version = 0
        review_version = 0
        for cycle in cycles:
            patch_version = await persist_patch(
                self._session,
                artifacts_root,
                task_id,
                cycle.proposal,
                cycle.applied.diff,
            )
            test_version = await persist_test_report(
                self._session,
                artifacts_root,
                task_id,
                cycle.tests,
            )
            review_version = await persist_review(
                self._session,
                artifacts_root,
                task_id,
                cycle.review,
                policy=cycle.policy,
                repair_count=cycle.repair_index,
            )
        patch_meta = stored.get("patch")
        if isinstance(patch_meta, dict):
            stored["patch"] = {**patch_meta, "version": patch_version}
        tests_meta = stored.get("tests")
        if isinstance(tests_meta, dict):
            stored["tests"] = {
                **tests_meta,
                "version": test_version,
                "checksum": checksum(test_report_document(cycles[-1].tests)),
            }
        review_meta = stored.get("review")
        if isinstance(review_meta, dict):
            stored["review"] = {**review_meta, "version": review_version}
        return stored

    async def _emit_routes(
        self,
        task_id: UUID,
        claimed: ClaimedTask,
        routes: tuple[dict[str, Any], ...],
        emit: Any,
    ) -> None:
        for item in await self._unwritten_routes(task_id, routes):
            await emit(
                stage=_STAGE_SUPERVISOR,
                event_type=ROUTE,
                payload=_route_payload(item, claimed.attempt),
                key=item["key"],
                run_id=claimed.run_id,
            )

    async def _unwritten_routes(
        self,
        task_id: UUID,
        routes: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        records = route_records(routes)
        if not records:
            return []
        keys = [route_event_key(task_id, index) for index, _item in enumerate(records, start=1)]
        found = set(
            await self._session.scalars(
                select(TaskEvent.idempotency_key).where(TaskEvent.idempotency_key.in_(keys))
            )
        )
        pending: list[dict[str, Any]] = []
        for index, item in enumerate(records, start=1):
            key = keys[index - 1]
            if key in found:
                continue
            pending.append({**item, "index": index, "key": key})
        return pending

    async def _stage_completed(self, task_id: UUID, stage: str) -> bool:
        found = await self._session.scalar(
            select(TaskEvent.id)
            .where(
                TaskEvent.task_id == task_id,
                TaskEvent.stage == stage,
                TaskEvent.event_type == STAGE_COMPLETED,
            )
            .limit(1)
        )
        return found is not None

    async def _insert_run(
        self,
        *,
        run_id: UUID,
        task_id: UUID,
        attempt: int,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        await self._session.execute(
            insert(TaskRun).values(
                id=run_id,
                task_id=task_id,
                attempt=attempt,
                worker_id=worker_id,
                started_at=func.now(),
                lease_until=_lease_deadline(lease_seconds),
            )
        )

    async def _next_attempt(self, task_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(TaskRun.attempt)).where(TaskRun.task_id == task_id)
        )
        return int(current or 0) + 1


def _opened_publication(result_json: dict[str, Any]) -> dict[str, Any] | None:
    """The draft identity. The token and the diff are not event fields."""
    publication = result_json.get("publication")
    if not isinstance(publication, dict) or publication.get("decision") != "approve":
        return None
    number = publication.get("number")
    url = publication.get("url")
    branch = publication.get("branch")
    owner = publication.get("owner")
    name = publication.get("name")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    if not all(isinstance(item, str) and item for item in (url, branch, owner, name)):
        return None
    return {
        "owner": owner,
        "name": name,
        "number": number,
        "branch": branch,
        "url": url,
        "draft": True,
    }


def _plan_payload(result_json: dict[str, Any], attempt: int) -> dict[str, Any] | None:
    """Paths and the decision only. The plan body stays in the artifact file."""
    plan = result_json.get("plan")
    if not isinstance(plan, dict):
        return None
    raw_paths = plan.get("paths")
    paths = (
        [item for item in raw_paths if isinstance(item, str)] if isinstance(raw_paths, list) else []
    )
    return {
        "attempt": attempt,
        "can_proceed": plan.get("can_proceed") is True,
        "version": plan.get("version"),
        "rejected_paths": plan.get("rejected_paths"),
        "paths": paths,
    }


def _tests_payload(result_json: dict[str, Any], attempt: int) -> dict[str, Any] | None:
    """The exit code and checksum only. Captured output stays in the artifact file."""
    tests = result_json.get("tests")
    if not isinstance(tests, dict):
        return None
    return {
        "attempt": attempt,
        "passed": tests.get("passed") is True,
        "status": tests.get("status"),
        "exit_code": tests.get("exit_code"),
        "timed_out": tests.get("timed_out") is True,
        "version": tests.get("version"),
        "checksum": tests.get("checksum"),
    }


def _patch_payload(result_json: dict[str, Any], attempt: int) -> dict[str, Any] | None:
    """Checksums and paths only. The diff and the proposal body stay in artifact files."""
    patch = result_json.get("patch")
    if not isinstance(patch, dict):
        return None
    raw_paths = patch.get("paths")
    paths = (
        [item for item in raw_paths if isinstance(item, str)] if isinstance(raw_paths, list) else []
    )
    return {
        "attempt": attempt,
        "paths": paths,
        "checksum": patch.get("checksum"),
        "proposal_checksum": patch.get("proposal_checksum"),
        "version": patch.get("version"),
    }


def _review_payload(result_json: dict[str, Any], attempt: int) -> dict[str, Any] | None:
    """The verdict and the policy. The diff and the findings stay in the artifact."""
    review = result_json.get("review")
    if not isinstance(review, dict):
        return None
    return {
        "attempt": attempt,
        "action": review.get("action"),
        "policy": review.get("policy"),
        "repair_count": review.get("repair_count"),
        "critical": review.get("critical") is True,
        "checksum": review.get("checksum"),
        "version": review.get("version"),
    }


def _route_payload(item: dict[str, Any], attempt: int) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "index": item["index"],
        "route": item["route"],
        "reason": item["reason"],
        "phase": item["phase"],
    }


def _repair_payloads(result_json: dict[str, Any], attempt: int) -> list[dict[str, Any]]:
    """One reason per repair. The list is empty when the first review was accepted."""
    raw = result_json.get("repairs")
    if not isinstance(raw, list):
        return []
    found: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        reason = item.get("reason")
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 2:
            continue
        if not isinstance(reason, str) or not reason.strip():
            continue
        found.append({"attempt": attempt, "index": index, "reason": reason})
    return found


def _investigation_payload(result_json: dict[str, Any], attempt: int) -> dict[str, Any] | None:
    """Citation labels only. File text and rejected paths stay out of the audit row."""
    investigation = result_json.get("investigation")
    if not isinstance(investigation, dict):
        return None
    report = investigation.get("report")
    if not isinstance(report, dict):
        return None
    citations: list[str] = []
    evidence = report.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            label = item.get("citation")
            if isinstance(label, str) and label.strip():
                citations.append(label)
    return {
        "attempt": attempt,
        "tool_calls": investigation.get("tool_calls"),
        "rejected_citations": investigation.get("rejected_citations"),
        "citations": citations,
    }


def _lease_deadline(lease_seconds: float):
    return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)
