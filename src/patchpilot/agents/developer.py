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
from patchpilot.patching.apply import AppliedPatch, apply_patch
from patchpilot.patching.errors import PatchError
from patchpilot.patching.policy import path_is_writable
from patchpilot.source.errors import SourceError
from patchpilot.source.paths import relative_parts

_DESCRIPTION_CHARS = 4_000
_FILE_CHARS = 8_000


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
    ) -> DevelopOutcome:
        self._check_lease()
        proposal = self._provider.generate_structured(
            _prompt(
                title=title,
                description=description,
                plan=plan,
                report=report,
                worktree=self._worktree,
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
) -> str:
    text = description.strip()
    if len(text) > _DESCRIPTION_CHARS:
        text = text[:_DESCRIPTION_CHARS]
    paths = "\n".join(f"- {item.relative_path} ({item.kind.value})" for item in plan.modifications)
    prompt = (
        "Write a patch for the approved plan. Do not run tests or a shell.\n"
        "Return file edits, or one unified diff, and no other paths.\n"
        "For an existing file, set expected_sha256 to the SHA256 shown below.\n"
        "For a new file, set expected_sha256 to an empty string.\n\n"
        f"Title: {title.strip()}\n"
        f"Description:\n{text}\n\n"
        f"Problem: {plan.problem_statement}\n"
        f"Approved paths:\n{paths or 'none'}\n"
        f"Evidence:\n{_evidence(report)}\n\n"
        f"{_file_blocks(worktree, plan)}"
    )
    return prompt[:MAX_PROMPT_CHARS]


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
