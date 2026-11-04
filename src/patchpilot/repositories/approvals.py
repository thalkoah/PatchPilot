"""Human decisions on a stored plan. The worker pauses; this module records the answer."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.audit import APPROVAL_DECIDED, approval_decided_key
from patchpilot.db.models import (
    APPROVAL_REASON_MAX_LENGTH,
    Approval,
    ApprovalStatus,
    Task,
    TaskStatus,
)
from patchpilot.repositories.events import append_event

# The graph node that called interrupt(). Kept in sync with workflow.graph.STAGE_APPROVAL.
_APPROVAL_STAGE = "request_plan_approval"


class ApprovalNotFound(Exception):
    """The task or the approval row is missing, or the row belongs to another task."""


class ApprovalConflict(Exception):
    """The stored decision does not match this submit, or the task is not waiting."""


@dataclass(frozen=True, slots=True)
class ApprovalView:
    """API-facing approval. It stays valid after the session rolls back."""

    id: UUID
    task_id: UUID
    status: ApprovalStatus
    plan_version: int
    reason: str | None
    created_at: datetime
    decided_at: datetime | None


class ApprovalRepository:
    """Reads approval rows and records one decision. The worker inserts the pending row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_task(self, task_id: UUID) -> list[Approval]:
        """Oldest first. Pending and decided rows are both returned."""
        statement = (
            select(Approval)
            .where(Approval.task_id == task_id)
            .order_by(Approval.created_at.asc(), Approval.id.asc())
        )
        return list(await self._session.scalars(statement))

    async def resume_value(self, task_id: UUID) -> dict[str, Any] | None:
        """The decision a worker passes to ``Command(resume=...)``, or None while pending."""
        row = await self._session.scalar(
            select(Approval)
            .where(
                Approval.task_id == task_id,
                Approval.status != ApprovalStatus.PENDING,
            )
            .order_by(Approval.decided_at.desc(), Approval.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        decision = "approve" if row.status == ApprovalStatus.APPROVED else "reject"
        return {
            "decision": decision,
            "approval_id": str(row.id),
            "reason": row.reason,
        }

    async def decide(
        self,
        task_id: UUID,
        approval_id: UUID,
        decision: str,
        reason: str | None,
    ) -> ApprovalView:
        """Store one decision and queue the task for resume.

        The same decision again returns the stored row and does not queue a second run.
        A different decision raises `ApprovalConflict`.
        """
        status = _status_for(decision)
        cleaned = _clean_reason(reason)
        task = await self._session.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            await self._session.rollback()
            raise ApprovalNotFound
        approval = await self._session.scalar(
            select(Approval).where(Approval.id == approval_id).with_for_update()
        )
        if approval is None or approval.task_id != task_id:
            await self._session.rollback()
            raise ApprovalNotFound
        if approval.status != ApprovalStatus.PENDING:
            if approval.status == status:
                view = _view(approval)
                await self._session.rollback()
                return view
            await self._session.rollback()
            raise ApprovalConflict
        if task.status != TaskStatus.WAITING_APPROVAL:
            await self._session.rollback()
            raise ApprovalConflict
        approval.status = status
        approval.reason = cleaned
        approval.decided_at = datetime.now(UTC)
        task.status = TaskStatus.QUEUED
        payload: dict[str, Any] = {
            "decision": decision,
            "approval_id": str(approval.id),
            "plan_version": approval.plan_version,
        }
        if cleaned is not None:
            payload["reason"] = cleaned
        await append_event(
            self._session,
            task_id=task.id,
            run_id=None,
            stage=_APPROVAL_STAGE,
            event_type=APPROVAL_DECIDED,
            payload=payload,
            idempotency_key=approval_decided_key(task.id),
            ordinal=0,
        )
        await self._session.commit()
        return _view(approval)


async def insert_pending_approval(
    session: AsyncSession,
    *,
    task_id: UUID,
    plan_version: int,
) -> Approval:
    """Insert the open request, or return the one already stored. The caller commits."""
    existing = await session.scalar(
        select(Approval).where(
            Approval.task_id == task_id,
            Approval.status == ApprovalStatus.PENDING,
        )
    )
    if existing is not None:
        return existing
    row = Approval(
        id=uuid4(),
        task_id=task_id,
        status=ApprovalStatus.PENDING,
        plan_version=plan_version,
    )
    session.add(row)
    await session.flush()
    return row


def _status_for(decision: str) -> ApprovalStatus:
    if decision == "approve":
        return ApprovalStatus.APPROVED
    if decision == "reject":
        return ApprovalStatus.REJECTED
    raise ValueError("decision must be approve or reject")


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise ValueError("reason must be text")
    text = " ".join(reason.split())
    if not text:
        return None
    if len(text) > APPROVAL_REASON_MAX_LENGTH:
        raise ValueError("reason is too long")
    return text


def _view(row: Approval) -> ApprovalView:
    return ApprovalView(
        id=row.id,
        task_id=row.task_id,
        status=row.status,
        plan_version=row.plan_version,
        reason=row.reason,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )
