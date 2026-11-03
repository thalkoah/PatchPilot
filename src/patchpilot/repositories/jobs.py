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

Events are append-only. Repeating a write with the same idempotency key does
not change the stored payload.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.artifacts import ArtifactStoreError
from patchpilot.audit import (
    ERROR_LEASE_EXPIRED,
    ERROR_MODEL,
    ERROR_RECOVERY_EXHAUSTED,
    MODEL_ERROR,
    RECOVERY,
    RECOVERY_EXHAUSTED,
    RECOVERY_RESUME,
    RETRY,
    STAGE_COMPLETED,
    STAGE_STARTED,
    TASK_COMPLETED,
    event_key,
    require_attempts,
    require_lease,
    require_stage,
    require_worker_id,
    task_completed_key,
)
from patchpilot.db.models import Task, TaskRun, TaskStatus
from patchpilot.llm.plan import ChangePlan
from patchpilot.repositories.artifacts import persist_plan
from patchpilot.repositories.events import append_event

# Stage names written when the graph finishes. Investigate is present only when
# the task named a repository and the report was stored.
_STAGE_TRIAGE = "triage"
_STAGE_INVESTIGATE = "investigate"
_STAGE_PLAN = "plan"
_STAGE_FINALIZE = "finalize"


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
        artifacts_root: Path | None = None,
    ) -> bool:
        """Store a terminal outcome when this worker still holds the lease.

        Return False when another worker has taken the run. The caller must not
        treat that as a failure of the task. A completed plan is written to the
        artifact volume before this commit. `ArtifactStoreError` means nothing
        was committed.
        """
        if status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            raise ValueError("triage persistence only accepts completed or failed")
        checked_stage = require_stage(stage)
        if not result_summary.strip():
            raise ValueError("result_summary must not be blank")
        task = await self._session.scalar(
            select(Task).where(Task.id == claimed.id).with_for_update()
        )
        if task is None or task.status != TaskStatus.RUNNING:
            await self._session.rollback()
            return False
        owned = await self._session.scalar(
            select(TaskRun.id).where(
                TaskRun.id == claimed.run_id,
                TaskRun.worker_id == claimed.worker_id,
                TaskRun.ended_at.is_(None),
                TaskRun.lease_until > func.now(),
            )
        )
        if owned is None:
            await self._session.rollback()
            return False
        stored = dict(result_json)
        if plan is not None and status == TaskStatus.COMPLETED:
            if artifacts_root is None:
                raise ArtifactStoreError
            version = await persist_plan(self._session, artifacts_root, claimed.id, plan)
            meta = stored.get("plan")
            plan_meta = dict(meta) if isinstance(meta, dict) else {}
            plan_meta["version"] = version
            stored["plan"] = plan_meta
        task.status = status
        task.stage = checked_stage
        task.result_summary = result_summary
        task.result_json = stored
        await self._session.execute(
            update(TaskRun)
            .where(TaskRun.id == claimed.run_id)
            .values(
                ended_at=func.now(),
                error_code=None if status == TaskStatus.COMPLETED else ERROR_MODEL,
            )
        )
        if status == TaskStatus.FAILED:
            await append_event(
                self._session,
                task_id=task.id,
                run_id=claimed.run_id,
                stage=checked_stage,
                event_type=MODEL_ERROR,
                payload={
                    "attempt": claimed.attempt,
                    "error_code": ERROR_MODEL,
                    "message": result_summary,
                },
                idempotency_key=event_key(
                    task_id=task.id,
                    run_id=claimed.run_id,
                    event_type=MODEL_ERROR,
                    stage=checked_stage,
                ),
                ordinal=0,
            )
        else:
            await self._record_success(task.id, claimed, stored)
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

    async def _record_success(
        self,
        task_id: UUID,
        claimed: ClaimedTask,
        result_json: dict[str, Any],
    ) -> None:
        attempt = {"attempt": claimed.attempt}
        ordinal = 0
        await append_event(
            self._session,
            task_id=task_id,
            run_id=claimed.run_id,
            stage=_STAGE_TRIAGE,
            event_type=STAGE_COMPLETED,
            payload=attempt,
            idempotency_key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=STAGE_COMPLETED,
                stage=_STAGE_TRIAGE,
            ),
            ordinal=ordinal,
        )
        ordinal += 1
        investigation = _investigation_payload(result_json, claimed.attempt)
        if investigation is not None:
            await append_event(
                self._session,
                task_id=task_id,
                run_id=claimed.run_id,
                stage=_STAGE_INVESTIGATE,
                event_type=STAGE_STARTED,
                payload=attempt,
                idempotency_key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_INVESTIGATE,
                ),
                ordinal=ordinal,
            )
            ordinal += 1
            await append_event(
                self._session,
                task_id=task_id,
                run_id=claimed.run_id,
                stage=_STAGE_INVESTIGATE,
                event_type=STAGE_COMPLETED,
                payload=investigation,
                idempotency_key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_INVESTIGATE,
                ),
                ordinal=ordinal,
            )
            ordinal += 1
        plan = _plan_payload(result_json, claimed.attempt)
        if plan is not None:
            await append_event(
                self._session,
                task_id=task_id,
                run_id=claimed.run_id,
                stage=_STAGE_PLAN,
                event_type=STAGE_STARTED,
                payload=attempt,
                idempotency_key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_STARTED,
                    stage=_STAGE_PLAN,
                ),
                ordinal=ordinal,
            )
            ordinal += 1
            await append_event(
                self._session,
                task_id=task_id,
                run_id=claimed.run_id,
                stage=_STAGE_PLAN,
                event_type=STAGE_COMPLETED,
                payload=plan,
                idempotency_key=event_key(
                    task_id=task_id,
                    run_id=claimed.run_id,
                    event_type=STAGE_COMPLETED,
                    stage=_STAGE_PLAN,
                ),
                ordinal=ordinal,
            )
            ordinal += 1
        await append_event(
            self._session,
            task_id=task_id,
            run_id=claimed.run_id,
            stage=_STAGE_FINALIZE,
            event_type=STAGE_STARTED,
            payload=attempt,
            idempotency_key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=STAGE_STARTED,
                stage=_STAGE_FINALIZE,
            ),
            ordinal=ordinal,
        )
        ordinal += 1
        await append_event(
            self._session,
            task_id=task_id,
            run_id=claimed.run_id,
            stage=_STAGE_FINALIZE,
            event_type=STAGE_COMPLETED,
            payload=attempt,
            idempotency_key=event_key(
                task_id=task_id,
                run_id=claimed.run_id,
                event_type=STAGE_COMPLETED,
                stage=_STAGE_FINALIZE,
            ),
            ordinal=ordinal,
        )
        ordinal += 1
        await append_event(
            self._session,
            task_id=task_id,
            run_id=None,
            stage=_STAGE_FINALIZE,
            event_type=TASK_COMPLETED,
            payload={**attempt, "status": TaskStatus.COMPLETED.value},
            idempotency_key=task_completed_key(task_id),
            ordinal=ordinal,
        )

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
