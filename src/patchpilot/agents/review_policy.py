"""Deterministic review policy. The reviewer recommendation is advisory.

Success requires no new test failure, a diff that stays inside the approved
plan, and no critical finding outside the baseline. A suite that only repeats
baseline failures can be accepted. A new failure or a repair request can run
the developer again, at most twice. Escalation, a policy breach, and an
exhausted budget end as a visible failure for a person to handle. This module
does not write files and does not call a model.
"""

import re
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
BASELINE_ACCEPT_REASON = "No new test failures. The remaining failure is in the baseline."
INTERVENTION = "A person needs to intervene."
_FAILED = re.compile(r"(?:^|\s)FAILED\s+(\S+)")
_COLLECTING = re.compile(r"ERROR collecting\s+(\S+)")
_ERROR = re.compile(r"(?:^|\s)ERROR\s+(\S+)")


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
    no_new_failures: bool = False,
    baseline_output: str = "",
    diff: str = "",
) -> PolicyDecision:
    """Choose accept, one more repair, or a visible failure."""
    if not isinstance(review, ReviewResult) or not isinstance(tests_passed, bool):
        raise ValueError("review policy input is not valid")
    if not isinstance(patch_allowed, bool) or not isinstance(no_new_failures, bool):
        raise ValueError("review policy input is not valid")
    if not isinstance(baseline_output, str) or not isinstance(diff, str):
        raise ValueError("review policy input is not valid")
    count = _count(repair_count)
    budget = require_repair_budget(max_repairs)
    critical = any(item.severity is FindingSeverity.CRITICAL for item in review.findings)
    if not patch_allowed:
        return _fail(PATCH_POLICY_REASON)
    if review.action is ReviewAction.ESCALATE:
        return _fail(ESCALATE_REASON)
    if _baseline_remainder(
        review,
        tests_passed=tests_passed,
        no_new_failures=no_new_failures,
        baseline_output=baseline_output,
        diff=diff,
    ):
        return PolicyDecision(action=PolicyAction.ACCEPT, reason=_bound(BASELINE_ACCEPT_REASON))
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


def failure_identities(stdout: str) -> tuple[str, ...]:
    """Pytest node ids and collection paths. The same line is kept once."""
    if not isinstance(stdout, str) or "\x00" in stdout:
        return ()
    found: list[str] = []
    for line in stdout.splitlines():
        collecting = _COLLECTING.search(line)
        if collecting:
            _remember(found, f"ERROR collecting {collecting.group(1)}")
            continue
        failed = _FAILED.search(line)
        if failed:
            _remember(found, f"FAILED {failed.group(1)}")
            continue
        error = _ERROR.search(line)
        if error is None or error.group(1) == "collecting":
            continue
        token = error.group(1)
        if "/" in token or "::" in token:
            _remember(found, f"ERROR {token}")
    return tuple(found)


def has_new_failure(
    *,
    baseline_stdout: str,
    patched_stdout: str,
    patched_passed: bool,
    patched_timed_out: bool,
    patched_truncated: bool,
) -> bool:
    """True when the patched run adds a failure the baseline did not have."""
    if not isinstance(patched_passed, bool) or not isinstance(patched_timed_out, bool):
        return True
    if not isinstance(patched_truncated, bool):
        return True
    if patched_passed:
        return False
    if patched_timed_out:
        return True
    patched = failure_identities(patched_stdout)
    if not patched:
        return True
    known = set(failure_identities(baseline_stdout))
    return any(item not in known for item in patched)


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


def _baseline_remainder(
    review: ReviewResult,
    *,
    tests_passed: bool,
    no_new_failures: bool,
    baseline_output: str,
    diff: str,
) -> bool:
    """True when every finding only restates a failure the baseline already had."""
    if tests_passed or not no_new_failures or not review.findings:
        return False
    return all(
        _evidence_is_baseline(item.evidence, baseline_output, diff) for item in review.findings
    )


def _evidence_is_baseline(evidence: str, baseline_output: str, diff: str) -> bool:
    text = evidence.strip()
    if not text or text not in baseline_output:
        return False
    return text not in diff


def _remember(found: list[str], identity: str) -> None:
    if identity not in found:
        found.append(identity)


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
