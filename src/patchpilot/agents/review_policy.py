"""Deterministic review policy. The reviewer recommendation is advisory.

Success requires a passing test report, a diff that stays inside the approved
plan, and no critical finding. A failing suite or a repair request can run the
developer again, at most twice. Escalation, a policy breach, and an exhausted
budget end as a visible failure for a person to handle. This module does not
write files and does not call a model.
"""

from dataclasses import dataclass
from enum import StrEnum

from patchpilot.agents.limits import DEFAULT_MAX_REPAIRS, require_repair_budget
from patchpilot.llm.plan import ChangePlan
from patchpilot.llm.review import FindingSeverity, ReviewAction, ReviewResult
from patchpilot.patching.apply import MAX_DIFF_BYTES
from patchpilot.patching.errors import PatchError
from patchpilot.patching.policy import approved_modification

_REASON_CHARS = 300
PATCH_POLICY_REASON = "The patch violates the edit policy."
ESCALATE_REASON = "The reviewer asked for a person to decide."
TEST_LIMIT_REASON = "Tests failed and the repair limit is exhausted."
CRITICAL_REASON = "A critical review finding blocks the patch."
REPAIR_LIMIT_REASON = "Repair stopped after 2 attempts."
INTERVENTION = "A person needs to intervene."


class PolicyAction(StrEnum):
    """What the workflow does after a review. This is not the model's action."""

    ACCEPT = "accept"
    REPAIR = "repair"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The authoritative next step and the reason recorded for a person."""

    action: PolicyAction
    reason: str


def decide_review(
    *,
    review: ReviewResult,
    tests_passed: bool,
    patch_allowed: bool,
    repair_count: int,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
) -> PolicyDecision:
    """Choose accept, one more repair, or a visible failure."""
    if not isinstance(review, ReviewResult) or not isinstance(tests_passed, bool):
        raise ValueError("review policy input is not valid")
    if not isinstance(patch_allowed, bool):
        raise ValueError("review policy input is not valid")
    count = _count(repair_count)
    budget = require_repair_budget(max_repairs)
    critical = any(item.severity is FindingSeverity.CRITICAL for item in review.findings)
    if not patch_allowed:
        return _fail(PATCH_POLICY_REASON)
    if review.action is ReviewAction.ESCALATE:
        return _fail(ESCALATE_REASON)
    if critical and review.action is not ReviewAction.REPAIR:
        return _fail(CRITICAL_REASON)
    if review.action is ReviewAction.ACCEPT and tests_passed and not critical:
        return PolicyDecision(action=PolicyAction.ACCEPT, reason=_bound(review.summary))
    if count < budget:
        return PolicyDecision(
            action=PolicyAction.REPAIR,
            reason=_repair_reason(review, tests_passed=tests_passed, critical=critical),
        )
    if not tests_passed:
        return _fail(TEST_LIMIT_REASON)
    if review.action is ReviewAction.REPAIR and not critical:
        return _fail(REPAIR_LIMIT_REASON)
    return _fail(CRITICAL_REASON)


def applied_diff_is_allowed(diff: str, plan: ChangePlan) -> bool:
    """True when every diff path is an approved plan path and the diff is small."""
    if not isinstance(diff, str) or not isinstance(plan, ChangePlan):
        return False
    if not diff.startswith("diff --git ") or "\x00" in diff:
        return False
    if len(diff.encode()) > MAX_DIFF_BYTES:
        return False
    paths = _diff_paths(diff)
    if not paths:
        return False
    for path in paths:
        try:
            approved_modification(plan, path)
        except PatchError:
            return False
    return True


def _repair_reason(review: ReviewResult, *, tests_passed: bool, critical: bool) -> str:
    if not tests_passed:
        lead = "Tests failed."
    elif critical:
        lead = "A critical finding needs a revision."
    else:
        lead = "The reviewer requested a revision."
    return _bound(f"{lead} {review.summary}")


def _fail(reason: str) -> PolicyDecision:
    text = reason.rstrip()
    if INTERVENTION.casefold() not in text.casefold():
        text = f"{text} {INTERVENTION}"
    return PolicyDecision(action=PolicyAction.FAIL, reason=_bound(text))


def _count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("repair count is not valid")
    return value


def _bound(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        return INTERVENTION
    return text[:_REASON_CHARS]


def _diff_paths(diff: str) -> list[str]:
    paths: list[str] = []
    marker = "diff --git a/"
    for line in diff.splitlines():
        if not line.startswith(marker) or " b/" not in line:
            continue
        left, right = line[len(marker) :].split(" b/", 1)
        if left != right or not left or left in paths:
            continue
        paths.append(left)
    return paths
