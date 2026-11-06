"""Developer agent. It proposes a patch and applies it only in the private worktree.

The model returns a `PatchProposal`. `apply_patch` checks that proposal against
the approved plan and writes the worktree. The proposal and the applied diff
stay separate: one is the model document, the other is the diff Git recorded.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchpilot.agents.errors import StopForLease
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.limits import MAX_PROMPT_CHARS
from patchpilot.llm.patch import PatchProposal
from patchpilot.llm.plan import ChangePlan, ModificationKind
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.review import ReviewResult
from patchpilot.patching.apply import AppliedPatch, apply_patch
from patchpilot.patching.errors import PatchError
from patchpilot.patching.policy import path_is_writable
from patchpilot.runner.protocol import TestReport
from patchpilot.source.errors import SourceError
from patchpilot.source.paths import relative_parts

_DESCRIPTION_CHARS = 4_000
_FILE_CHARS = 8_000
_OUTPUT_CHARS = 500


@dataclass(frozen=True, slots=True)
class DevelopOutcome:
    """The model proposal and the diff that was applied. They are not the same bytes."""

    proposal: PatchProposal
    applied: AppliedPatch

    def as_state(self) -> dict[str, Any]:
        return {
            "summary": self.applied.summary,
            "paths": list(self.applied.paths),
            "diff": self.applied.diff.decode("utf-8"),
            "proposal": self.proposal.model_dump(mode="json"),
        }


class DeveloperAgent:
    """One structured call, then the deterministic apply_patch tool."""

    def __init__(
        self,
        provider: LLMProvider,
        worktree: Path,
        *,
        lease_lost: Callable[[], bool] | None = None,
    ) -> None:
        self._provider = provider
        self._worktree = worktree
        self._lease_lost = lease_lost

    def run(
        self,
        *,
        title: str,
        description: str,
        plan: ChangePlan,
        report: InvestigationReport,
        review: ReviewResult | None = None,
        repair_count: int = 0,
        tests: TestReport | None = None,
    ) -> DevelopOutcome:
        self._check_lease()
        proposal = self._provider.generate_structured(
            _prompt(
                title=title,
                description=description,
                plan=plan,
                report=report,
                worktree=self._worktree,
                review=review,
                repair_count=repair_count,
                tests=tests,
            ),
            PatchProposal,
        )
        self._check_lease()
        applied = apply_patch(worktree=self._worktree, proposal=proposal, plan=plan)
        return DevelopOutcome(proposal=proposal, applied=applied)

    def _check_lease(self) -> None:
        if self._lease_lost is not None and self._lease_lost():
            raise StopForLease


def _prompt(
    *,
    title: str,
    description: str,
    plan: ChangePlan,
    report: InvestigationReport,
    worktree: Path,
    review: ReviewResult | None,
    repair_count: int,
    tests: TestReport | None,
) -> str:
    text = description.strip()
    if len(text) > _DESCRIPTION_CHARS:
        text = text[:_DESCRIPTION_CHARS]
    paths = "\n".join(f"- {item.relative_path} ({item.kind.value})" for item in plan.modifications)
    if repair_count > 0:
        instruction = (
            "Revise the previous patch. The files below are the previous attempt.\n"
            "An unchanged file is rejected.\n"
            "The next edit must stay on the approved paths.\n"
        )
    else:
        instruction = "Write a patch for the approved plan.\n"
    prompt = (
        f"{instruction}"
        "Do not run tests or a shell.\n"
        "Return file edits, or one unified diff, and no other paths.\n"
        "For an existing file, set expected_sha256 to the SHA256 shown below.\n"
        "For a new file, set expected_sha256 to an empty string.\n\n"
        f"Title: {title.strip()}\n"
        f"Description:\n{text}\n\n"
        f"Problem: {plan.problem_statement}\n"
        f"Approved paths:\n{paths or 'none'}\n"
        f"Evidence:\n{_evidence(report)}\n\n"
        f"Repair count: {repair_count}\n"
        f"{_review_block(review)}\n"
        f"{_repair_details(review, tests) if repair_count > 0 else ''}\n"
        f"{_file_blocks(worktree, plan)}"
    )
    return prompt[:MAX_PROMPT_CHARS]


def _review_block(review: ReviewResult | None) -> str:
    if review is None:
        return "Reviewer action: none"
    return f"Reviewer action: {review.action.value}\nReviewer summary: {review.summary}"


def _repair_details(review: ReviewResult | None, tests: TestReport | None) -> str:
    """Findings and the failing output. A repair that ignores both can echo the file."""
    if review is None or not review.findings:
        findings = "none"
    else:
        findings = "\n".join(f"- {item.severity.value}: {item.summary}" for item in review.findings)
    excerpt = "none"
    if tests is not None:
        captured = tests.stdout.strip() or tests.stderr.strip()
        if captured:
            excerpt = captured[:_OUTPUT_CHARS]
    return f"Findings:\n{findings}\nTest output:\n{excerpt}\n"


def _evidence(report: InvestigationReport) -> str:
    lines = [item.citation for item in report.evidence]
    return "\n".join(lines) or "none"


def _file_blocks(worktree: Path, plan: ChangePlan) -> str:
    blocks: list[str] = []
    for modification in plan.modifications:
        if not path_is_writable(worktree, modification):
            raise PatchError("denied_path")
        try:
            relative = "/".join(relative_parts(modification.relative_path, allow_root=False))
        except SourceError:
            raise PatchError("denied_path") from None
        candidate = worktree.joinpath(*relative.split("/"))
        if modification.kind is ModificationKind.NEW_TEST and not candidate.exists():
            blocks.append(f"FILE {relative} sha256=new")
            continue
        try:
            payload = candidate.read_bytes()
        except OSError:
            raise PatchError("denied_path") from None
        if b"\x00" in payload:
            raise PatchError("binary")
        if len(payload) > _FILE_CHARS:
            raise PatchError("oversized")
        try:
            body = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise PatchError("binary") from None
        digest = hashlib.sha256(payload).hexdigest()
        blocks.append(f"FILE {relative} sha256={digest}\n{body.rstrip()}")
    return "\n\n".join(blocks)
