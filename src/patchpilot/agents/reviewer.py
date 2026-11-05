"""Reviewer agent. It reads the plan, the applied diff, and the test report.

The model returns a `ReviewResult`. Evidence that does not appear in the diff,
the test report, or an investigation citation is replaced. This agent has no
file tool and cannot modify the worktree.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from patchpilot.agents.errors import StopForLease
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.limits import MAX_PROMPT_CHARS
from patchpilot.llm.patch import PatchProposal
from patchpilot.llm.plan import ChangePlan
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.review import ReviewFinding, ReviewResult
from patchpilot.patching.apply import AppliedPatch
from patchpilot.runner.protocol import TestReport

_DESCRIPTION_CHARS = 4_000
_DIFF_CHARS = 6_000
_OUTPUT_CHARS = 500
_UNVERIFIED = "The cited text was not in the diff or the test report."
_PATHISH = re.compile(r"[A-Za-z0-9_./-]{3,200}\.[A-Za-z0-9]{1,8}")


@dataclass(frozen=True, slots=True)
class ReviewedCycle:
    """One develop, test, and review pass. The diff is the applied patch."""

    proposal: PatchProposal
    applied: AppliedPatch
    tests: TestReport
    review: ReviewResult
    policy: str
    reason: str
    repair_index: int


class ReviewerAgent:
    """One structured call. The return value is not applied to the worktree."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        lease_lost: Callable[[], bool] | None = None,
    ) -> None:
        self._provider = provider
        self._lease_lost = lease_lost

    def run(
        self,
        *,
        title: str,
        description: str,
        plan: ChangePlan,
        report: InvestigationReport,
        diff: str,
        tests: TestReport,
        repair_count: int,
    ) -> ReviewResult:
        self._check_lease()
        draft = self._provider.generate_structured(
            _prompt(
                title=title,
                description=description,
                plan=plan,
                report=report,
                diff=diff,
                tests=tests,
                repair_count=repair_count,
            ),
            ReviewResult,
        )
        self._check_lease()
        return ground_review(draft, diff=diff, tests=tests, report=report)

    def _check_lease(self) -> None:
        if self._lease_lost is not None and self._lease_lost():
            raise StopForLease


def ground_review(
    review: ReviewResult,
    *,
    diff: str,
    tests: TestReport,
    report: InvestigationReport,
) -> ReviewResult:
    """Drop evidence the diff, the report, and the citations do not contain."""
    corpus = _corpus(diff=diff, tests=tests, report=report)
    summary = review.summary
    if _names_missing_path(summary, corpus):
        summary = _UNVERIFIED
    findings: list[ReviewFinding] = []
    for item in review.findings:
        evidence = item.evidence if _grounded(item.evidence, corpus) else _UNVERIFIED
        finding_summary = item.summary
        if _names_missing_path(finding_summary, corpus):
            finding_summary = _UNVERIFIED
        findings.append(
            ReviewFinding(summary=finding_summary, severity=item.severity, evidence=evidence)
        )
    evidence = [item for item in review.evidence if _grounded(item, corpus)]
    return ReviewResult(
        summary=summary,
        action=review.action,
        findings=findings,
        evidence=evidence,
    )


def _prompt(
    *,
    title: str,
    description: str,
    plan: ChangePlan,
    report: InvestigationReport,
    diff: str,
    tests: TestReport,
    repair_count: int,
) -> str:
    text = description.strip()
    if len(text) > _DESCRIPTION_CHARS:
        text = text[:_DESCRIPTION_CHARS]
    paths = "\n".join(f"- {item.relative_path}" for item in plan.modifications)
    citations = "\n".join(item.citation for item in report.evidence) or "none"
    shown = diff if len(diff) <= _DIFF_CHARS else diff[:_DIFF_CHARS]
    prompt = (
        "Review the applied diff and the test report. Do not edit files or run a shell.\n"
        "The diff below is the patch that was applied, not a new proposal.\n"
        "Return accept, repair, or escalate.\n\n"
        f"Title: {title.strip()}\n"
        f"Description:\n{text}\n\n"
        f"Problem: {plan.problem_statement}\n"
        f"Approved paths:\n{paths or 'none'}\n"
        f"Evidence:\n{citations}\n\n"
        f"Tests status: {tests.status.value}\n"
        f"Tests exit code: {tests.exit_code}\n"
        f"Repair count: {repair_count}\n\n"
        f"Diff:\n{shown}\n\n"
        f"Test output:\n{tests.stdout[:_OUTPUT_CHARS]}\n"
    )
    return prompt[:MAX_PROMPT_CHARS]


def _corpus(*, diff: str, tests: TestReport, report: InvestigationReport) -> str:
    parts = [
        diff,
        tests.status.value,
        tests.stdout,
        tests.stderr,
        *(item.citation for item in report.evidence),
        *(item.relative_path for item in report.evidence),
    ]
    return "\n".join(parts)


def _grounded(evidence: str, corpus: str) -> bool:
    text = evidence.strip()
    return len(text) >= 12 and text in corpus and not _names_missing_path(text, corpus)


def _names_missing_path(text: str, corpus: str) -> bool:
    return any(match.group(0) not in corpus for match in _PATHISH.finditer(text))
