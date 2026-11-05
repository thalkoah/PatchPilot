"""Event names, recovery error codes, and the payload bound for the audit log.

Payloads are untrusted. This module drops secret-like keys and refuses to store
a document larger than a few kilobytes. It does not talk to the database.
"""

import json
import math
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from patchpilot.db.models import (
    EVENT_TYPE_MAX_LENGTH,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    STAGE_MAX_LENGTH,
    WORKER_ID_MAX_LENGTH,
)

STAGE_STARTED = "stage_started"
STAGE_COMPLETED = "stage_completed"
MODEL_ERROR = "model_error"
TASK_COMPLETED = "task_completed"
TASK_REJECTED = "task_rejected"
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_DECIDED = "approval_decided"
RETRY = "retry"
RECOVERY = "recovery"
REPAIR = "repair"

EVENT_TYPES = frozenset(
    {
        STAGE_STARTED,
        STAGE_COMPLETED,
        MODEL_ERROR,
        TASK_COMPLETED,
        TASK_REJECTED,
        APPROVAL_REQUESTED,
        APPROVAL_DECIDED,
        RETRY,
        RECOVERY,
        REPAIR,
    }
)

ERROR_LEASE_EXPIRED = "lease_expired"
ERROR_RECOVERY_EXHAUSTED = "recovery_exhausted"
ERROR_MODEL = "model_error"

RECOVERY_RESUME = "resume"
RECOVERY_EXHAUSTED = "exhausted"

MAX_EVENT_PAGE = 500
MAX_EVENT_PAYLOAD_BYTES = 2048
MAX_EVENT_STRING = 300
MAX_EVENT_DEPTH = 4
MAX_EVENT_ITEMS = 20
REDACTED = "[redacted]"

_SECRET_PARTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "database_url",
)


def require_stage(stage: str) -> str:
    if (
        not isinstance(stage, str)
        or stage.strip() == ""
        or stage != stage.strip()
        or len(stage) > STAGE_MAX_LENGTH
    ):
        raise ValueError("stage must be a short non-blank name")
    return stage


def require_event_type(event_type: str) -> str:
    if event_type not in EVENT_TYPES or len(event_type) > EVENT_TYPE_MAX_LENGTH:
        raise ValueError("event_type is not one of the audit names")
    return event_type


def require_worker_id(worker_id: str) -> str:
    if (
        not isinstance(worker_id, str)
        or worker_id.strip() == ""
        or worker_id != worker_id.strip()
        or len(worker_id) > WORKER_ID_MAX_LENGTH
        or any(char.isspace() for char in worker_id)
    ):
        raise ValueError("worker_id must be a short non-blank id")
    return worker_id


def require_lease(lease_seconds: float) -> float:
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
        raise ValueError("lease_seconds must be a positive number")
    lease = float(lease_seconds)
    if not math.isfinite(lease) or lease <= 0 or lease > 3600:
        raise ValueError("lease_seconds must be between 0 and 3600 seconds")
    return lease


def require_attempts(max_attempts: int) -> int:
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("max_attempts must be from 1 to 20")
    if not 1 <= max_attempts <= 20:
        raise ValueError("max_attempts must be from 1 to 20")
    return max_attempts


def event_key(*, task_id: UUID, run_id: UUID | None, event_type: str, stage: str) -> str:
    """Stable identity for one audit fact. A second insert with this key is a no-op."""
    run = "-" if run_id is None else str(run_id)
    key = f"{task_id}:{run}:{event_type}:{stage}"
    if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValueError("idempotency key is too long")
    return key


def task_completed_key(task_id: UUID) -> str:
    """One completion row per task, including a replay of the same finish."""
    return _task_key(task_id, "task_completed")


def task_rejected_key(task_id: UUID) -> str:
    """One rejection row per task, including a replay of the same finish."""
    return _task_key(task_id, "task_rejected")


def approval_requested_key(task_id: UUID) -> str:
    """One pause row per task. Parking again after a crash does not insert another."""
    return _task_key(task_id, "approval_requested")


def repair_event_key(task_id: UUID, repair_index: int) -> str:
    """One repair row per index. A second finish of the same repair does not insert another."""
    if isinstance(repair_index, bool) or not isinstance(repair_index, int):
        raise ValueError("repair index is out of range")
    if not 1 <= repair_index <= 2:
        raise ValueError("repair index is out of range")
    return _task_key(task_id, f"repair:{repair_index}")


def approval_decided_key(task_id: UUID) -> str:
    """One decision row per task. A repeated submit does not insert another."""
    return _task_key(task_id, "approval_decided")


def _task_key(task_id: UUID, name: str) -> str:
    key = f"{task_id}:{name}"
    if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValueError("idempotency key is too long")
    return key


def bound_payload(value: object) -> dict[str, Any]:
    """Return a small JSON object. Secret-like keys are replaced, never stored."""
    if not isinstance(value, dict):
        return {}
    cleaned = _scrub(value, depth=0)
    if not isinstance(cleaned, dict):
        return {}
    if _fits(cleaned):
        return cleaned
    compact: dict[str, Any] = {"truncated": True}
    attempt = cleaned.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        compact["attempt"] = attempt
    return compact


def _fits(payload: Mapping[str, Any]) -> bool:
    try:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return len(encoded.encode()) <= MAX_EVENT_PAYLOAD_BYTES


def _scrub(value: object, *, depth: int) -> object:
    if depth > MAX_EVENT_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_EVENT_ITEMS:
                break
            name = str(key)[:80]
            if _secret_key(name):
                cleaned[name] = REDACTED
            else:
                cleaned[name] = _scrub(item, depth=depth + 1)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item, depth=depth + 1) for item in value[:MAX_EVENT_ITEMS]]
    if isinstance(value, str):
        return " ".join(value.split())[:MAX_EVENT_STRING]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    return REDACTED


def _secret_key(name: str) -> bool:
    folded = name.casefold().replace("-", "_")
    return any(part in folded for part in _SECRET_PARTS)
