"""Planner agent. It proposes a change and does not write the checkout.

A plan can proceed only when the investigation cited at least one line, triage
does not need clarification, and every surviving path is an existing `.py` file
or a permitted new test path. Anything else is stored as a decline.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchpilot.agents.errors import StopForLease
from patchpilot.agents.plan_paths import modification_is_allowed
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.limits import MAX_PROMPT_CHARS
from patchpilot.llm.plan import ChangePlan, ProposedModification
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.triage import TriageResult

_DESCRIPTION_CHARS = 4_000
_DECLINED = "The planner declined because the evidence does not support a change."
_KEPT_PATHS = "The planner kept only paths that are permitted in this repository."
_NO_TESTS = "No test change is proposed until the failure is reproducible."
_KEEP_TESTS = "Run the repository tests that cover the cited files."


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """The accepted plan and how many proposed paths were dropped."""

    plan: ChangePlan
    rejected_paths: int

    def as_state(self) -> dict[str, Any]:
        return {
            "plan": self.plan.model_dump(mode="json"),
            "rejected_paths": self.rejected_paths,
        }


class PlannerAgent:
    """One structured call, then a deterministic path and evidence check."""

    def __init__(
        self,
        provider: LLMProvider,
        checkout: Path,
        *,
        lease_lost: Callable[[], bool] | None = None,
    ) -> None:
        self._provider = provider
        self._checkout = checkout
        self._lease_lost = lease_lost

    def run(
        self,
        *,
        title: str,
        description: str,
        triage: TriageResult,
        report: InvestigationReport,
    ) -> PlanOutcome:
        self._check_lease()
        draft = self._provider.generate_structured(
            _prompt(title=title, description=description, triage=triage, report=report),
            ChangePlan,
        )
        accepted, rejected = accept_plan(
            draft,
            triage=triage,
            report=report,
            checkout=self._checkout,
        )
        return PlanOutcome(plan=accepted, rejected_paths=rejected)

    def _check_lease(self) -> None:
        if self._lease_lost is not None and self._lease_lost():
            raise StopForLease


def accept_plan(
    plan: ChangePlan,
    *,
    triage: TriageResult,
    report: InvestigationReport,
    checkout: Path,
) -> tuple[ChangePlan, int]:
    """Drop paths that are not permissible, and decline when the evidence is thin."""
    kept: list[ProposedModification] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for modification in plan.modifications:
        path = modification.relative_path
        if path in seen:
            continue
        seen.add(path)
        if modification_is_allowed(checkout, path, modification.kind):
            kept.append(modification)
        else:
            rejected.append(path)
    evidence_ok = bool(report.evidence) and not triage.needs_clarification
    can_proceed = bool(plan.can_proceed and evidence_ok and kept)
    if not can_proceed:
        kept = []
    statement = _clean(
        plan.problem_statement,
        rejected,
        fallback=_KEPT_PATHS if can_proceed else _DECLINED,
    )
    strategy = _clean(
        plan.test_strategy,
        rejected,
        fallback=_KEEP_TESTS if can_proceed else _NO_TESTS,
    )
    assumptions = [item for item in plan.assumptions if not _tainted(item, rejected)]
    risks = [item for item in plan.risks if not _tainted(item, rejected)]
    accepted = plan.model_copy(
        update={
            "problem_statement": statement,
            "assumptions": assumptions[:8],
            "modifications": kept,
            "test_strategy": strategy,
            "risks": risks[:8],
            "can_proceed": can_proceed,
        }
    )
    return accepted, len(rejected)


def _prompt(
    *,
    title: str,
    description: str,
    triage: TriageResult,
    report: InvestigationReport,
) -> str:
    text = description.strip()
    if len(text) > _DESCRIPTION_CHARS:
        text = text[:_DESCRIPTION_CHARS]
    evidence = "\n".join(item.citation for item in report.evidence) or "none"
    probable = "\n".join(report.probable_files) or "none"
    questions = "\n".join(report.unanswered_questions) or "none"
    prompt = (
        "Plan a minimal change from the investigation. Do not write files or include a diff.\n"
        "Set can_proceed false when the evidence is not enough to support a change.\n"
        'Use kind "existing" for a .py file that is already in the checkout.\n'
        'Use kind "new_test" for a new tests/test_*.py file under an existing tests directory.\n'
        "Do not propose any other path.\n\n"
        f"Title: {title.strip()}\n"
        f"Description:\n{text}\n\n"
        f"Triage: {triage.classification.value}, severity {triage.severity.value}. "
        f"{triage.summary}\n"
        f"Needs clarification: {str(triage.needs_clarification).lower()}\n\n"
        f"Investigation: {report.summary}\n"
        f"Evidence:\n{evidence}\n"
        f"Probable files:\n{probable}\n"
        f"Unanswered questions:\n{questions}\n"
    )
    return prompt[:MAX_PROMPT_CHARS]


def _clean(text: str, rejected: list[str], *, fallback: str) -> str:
    if _tainted(text, rejected):
        return fallback
    return text


def _tainted(text: str, rejected: list[str]) -> bool:
    return any(path and path in text for path in rejected)
