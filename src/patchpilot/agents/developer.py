"""Developer agent. It proposes a patch and applies it only in the private worktree.

The model returns a `DevelopDiff`, a few exact replacements. This agent renders
one unified diff and `apply_patch` checks that against the approved plan. The
proposal and the applied diff stay separate: one is the rendered document, the
other is the diff Git recorded.
"""

import difflib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchpilot.agents.errors import StopForLease
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.limits import MAX_PROMPT_CHARS
from patchpilot.llm.patch import DevelopDiff, PatchProposal, SnippetEdit
from patchpilot.llm.plan import ChangePlan, ModificationKind
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.review import ReviewResult
from patchpilot.patching.apply import AppliedPatch, apply_patch
from patchpilot.patching.errors import PatchError
from patchpilot.patching.policy import approved_modification, path_is_writable
from patchpilot.runner.protocol import TestReport
from patchpilot.source.errors import SourceError
from patchpilot.source.paths import relative_parts

_DESCRIPTION_CHARS = 4_000
_FILE_CHARS = 8_000
_OUTPUT_CHARS = 500
_SYNTAX_REFUSAL = "The previous edit was not valid Python.\n"
_MISMATCH_REFUSAL = (
    "The previous old text was not found once in the approved file.\n"
    "For an existing file, copy old verbatim from that file. An empty old is rejected.\n"
)
_SNIPPET_RULES = (
    "Copy a unique span into old and put the replacement in new.\n"
    "For an existing file, old must be copied verbatim from that file and must not be empty.\n"
    "old and new must be complete lines.\n"
    "An empty old is only for a new test file.\n"
    "Example:\n"
    'old: def health() -> dict[str, str]:\\n    return {"status": "ok"}\\n\n'
    'new: def health() -> dict[str, str]:\\n    return {"status": "ready"}\\n\n'
)


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
        refusal: str | None = None
        while True:
            self._check_lease()
            try:
                proposal = _as_proposal(
                    self._provider.generate_structured(
                        _prompt(
                            title=title,
                            description=description,
                            plan=plan,
                            report=report,
                            worktree=self._worktree,
                            review=review,
                            repair_count=repair_count,
                            tests=tests,
                            refusal=refusal,
                        ),
                        DevelopDiff,
                    ),
                    self._worktree,
                    plan,
                )
                self._check_lease()
                applied = apply_patch(worktree=self._worktree, proposal=proposal, plan=plan)
            except PatchError as exc:
                if exc.code not in {"syntax", "mismatch"} or refusal is not None:
                    raise
                refusal = exc.code
                continue
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
    refusal: str | None = None,
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
        instruction = "Write the approved change as exact text replacements.\n"
    if refusal == "syntax":
        refused = _SYNTAX_REFUSAL
    elif refusal == "mismatch":
        refused = _MISMATCH_REFUSAL
    else:
        refused = ""
    prompt = (
        f"{instruction}"
        f"{refused}"
        "Do not run tests or a shell.\n"
        f"{_SNIPPET_RULES}"
        "Do not return a diff or the full file.\n\n"
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


def _as_proposal(generated: object, worktree: Path, plan: ChangePlan) -> PatchProposal:
    """Store a develop reply as a proposal. A full-file fixture is already one."""
    if isinstance(generated, PatchProposal):
        return generated
    if isinstance(generated, DevelopDiff):
        return _proposal_from_snippets(generated, worktree, plan)
    raise TypeError("develop model did not return a patch")


def _proposal_from_snippets(
    generated: DevelopDiff,
    worktree: Path,
    plan: ChangePlan,
) -> PatchProposal:
    """Replace each unique span in memory and render the git diff apply_patch expects."""
    originals: dict[str, str | None] = {}
    current: dict[str, str] = {}
    order: list[str] = []
    for edit in generated.edits:
        modification = approved_modification(plan, edit.relative_path)
        if not path_is_writable(worktree, modification):
            raise PatchError("denied_path")
        try:
            path = "/".join(relative_parts(modification.relative_path, allow_root=False))
        except SourceError:
            raise PatchError("denied_path") from None
        if path not in current:
            original = _read_text(worktree, path)
            originals[path] = original
            current[path] = "" if original is None else original
            order.append(path)
        creating = (
            originals[path] is None
            and current[path] == ""
            and modification.kind is ModificationKind.NEW_TEST
        )
        current[path] = _replace_span(current[path], edit, creating=creating)
    chunks: list[str] = []
    for path in order:
        after = current[path]
        if after and not after.endswith("\n"):
            after += "\n"
        before = originals[path]
        if before == after:
            continue
        chunks.append(_render_git_diff(path, before, after))
    if not chunks:
        raise PatchError("empty")
    return PatchProposal(summary=generated.summary, unified_diff="".join(chunks))


def _replace_span(text: str, edit: SnippetEdit, *, creating: bool) -> str:
    if edit.old == "":
        if not creating or text:
            raise PatchError("mismatch")
        return edit.new
    if text.count(edit.old) != 1:
        raise PatchError("mismatch")
    return text.replace(edit.old, edit.new, 1)


def _read_text(worktree: Path, relative: str) -> str | None:
    candidate = worktree.joinpath(*relative.split("/"))
    if candidate.is_symlink():
        raise PatchError("denied_path")
    if not candidate.exists():
        return None
    try:
        payload = candidate.read_bytes()
    except OSError:
        raise PatchError("denied_path") from None
    if b"\x00" in payload:
        raise PatchError("binary")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchError("binary") from None


def _render_git_diff(path: str, before: str | None, after: str) -> str:
    """The header shape `apply_patch` accepts for a model diff."""
    if not after.endswith("\n"):
        after = f"{after}\n"
    body = "".join(
        difflib.unified_diff(
            [] if before is None else before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="/dev/null" if before is None else f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if not body.strip():
        raise PatchError("empty")
    if not body.endswith("\n"):
        body += "\n"
    header = f"diff --git a/{path} b/{path}\n"
    if before is None:
        header += "new file mode 100644\n"
    return header + body


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
