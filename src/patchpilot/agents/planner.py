"""Planner agent. It proposes a change and does not write the checkout.

A plan can proceed when triage does not need clarification and a cited existing
`.py` file survives. A draft that names such a file proceeds even when the model
set `can_proceed` false. A draft that does not name one proceeds only when the
model asked to, the investigation cited a line, and a permitted path remains.
Anything else is stored as a decline.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchpilot.agents.errors import StopForLease
from patchpilot.agents.plan_paths import modification_is_allowed
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.limits import MAX_PROMPT_CHARS
from patchpilot.llm.plan import ChangePlan, ModificationKind, ProposedModification
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.triage import TriageResult

_DESCRIPTION_CHARS = 4_000
_DECLINED = "The planner declined because the evidence does not support a change."
_KEPT_PATHS = "The planner kept only paths that are permitted in this repository."
_NO_TESTS = "No test change is proposed until the failure is reproducible."
_KEEP_TESTS = "Run the repository tests that cover the cited files."
_CITED_EDIT = "Edit the cited lines."
_MAX_MODIFICATIONS = 8


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
    named = [path for path in _cited_python_files(report, checkout) if _draft_names(plan, path)]
    if named and not triage.needs_clarification:
        present = {item.relative_path for item in kept}
        for path in named:
            if path in present or len(kept) >= _MAX_MODIFICATIONS:
                continue
            kept.append(
                ProposedModification(
                    relative_path=path,
                    kind=ModificationKind.EXISTING,
                    summary=_CITED_EDIT,
                )
            )
            present.add(path)
        can_proceed = True
    else:
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
        "Set can_proceed true when Needs clarification is false and at least one evidence "
        "line names an existing .py file. Add a modification for each such file with kind "
        '"existing".\n'
        "An unanswered question, a dropped citation, or a non-Python path does not cancel "
        "a line that was read.\n"
        "Set can_proceed false when Needs clarification is true or no evidence line names "
        "a .py file that can be edited.\n"
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


def _cited_python_files(report: InvestigationReport, checkout: Path) -> list[str]:
    """Evidence paths that may be edited as existing Python files, in report order."""
    found: list[str] = []
    seen: set[str] = set()
    for item in report.evidence:
        path = item.relative_path
        if path in seen:
            continue
        seen.add(path)
        if modification_is_allowed(checkout, path, ModificationKind.EXISTING):
            found.append(path)
    return found


def _draft_names(plan: ChangePlan, path: str) -> bool:
    """True when the statement, assumptions, test strategy, or risks contain this path."""
    pieces = (plan.problem_statement, plan.test_strategy, *plan.assumptions, *plan.risks)
    return any(path in piece for piece in pieces)


def _clean(text: str, rejected: list[str], *, fallback: str) -> str:
    if _tainted(text, rejected):
        return fallback
    return text


def _tainted(text: str, rejected: list[str]) -> bool:
    return any(path and path in text for path in rejected)
