"""Deterministic provider for tests and offline runs."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from patchpilot.llm.errors import LLMResponseError
from patchpilot.llm.investigation import (
    EvidenceCitation,
    Hypothesis,
    InvestigationReport,
    InvestigatorStep,
    ToolName,
    ToolRequest,
)
from patchpilot.llm.parsing import coerce_model, require_prompt, require_schema, resolve_options
from patchpilot.llm.patch import FileEdit, PatchProposal
from patchpilot.llm.plan import ChangePlan, ModificationKind, ProposedModification
from patchpilot.llm.provider import GenerateOptions, ModelT
from patchpilot.llm.review import FindingSeverity, ReviewAction, ReviewFinding, ReviewResult
from patchpilot.llm.supervisor import SupervisorRecommendation, SupervisorRoute
from patchpilot.llm.triage import Classification, Severity, TriageResult

Fixture = BaseModel | Mapping[str, Any]

DISCOUNT_TIER_TRIAGE = TriageResult(
    classification=Classification.BUG,
    severity=Severity.HIGH,
    summary="An unrecognized customer tier makes the discount endpoint return HTTP 500.",
    acceptance_criteria=[
        "An unknown customer tier returns HTTP 422.",
        "The response explains the validation error.",
        "A regression test covers an unrecognized tier.",
    ],
    confidence=0.75,
    needs_clarification=False,
)

NEEDS_CLARIFICATION_TRIAGE = TriageResult(
    classification=Classification.UNCLEAR,
    severity=Severity.LOW,
    summary="The report does not identify a reproducible failure.",
    acceptance_criteria=[
        "The reporter names the expected behavior and the actual behavior.",
    ],
    confidence=0.25,
    needs_clarification=True,
)

GENERIC_BUG_TRIAGE = TriageResult(
    classification=Classification.BUG,
    severity=Severity.MEDIUM,
    summary="The report describes a defect that needs a small, tested fix.",
    acceptance_criteria=[
        "The reported failure no longer occurs.",
        "A regression test covers the reported case.",
    ],
    confidence=0.5,
    needs_clarification=False,
)


@dataclass(frozen=True, slots=True)
class ProviderCall:
    prompt: str
    schema_name: str
    temperature: float


def builtin_triage(prompt: str) -> TriageResult:
    text = prompt.casefold()
    if "discount" in text and "tier" in text:
        return DISCOUNT_TIER_TRIAGE
    if "needs clarification" in text or "not enough information" in text:
        return NEEDS_CLARIFICATION_TRIAGE
    return GENERIC_BUG_TRIAGE


# The model fixture obeys this comment. The investigator must not store the path.
INJECTION_MARKER = "PATCHPILOT_INJECT"
INJECTION_PATH = "secrets/api_token.txt"
INJECTION_TOKEN = "injected-token-value"
BOGUS_PATH = "invented/not_read.py"

MISSING_EVIDENCE_REPORT = InvestigationReport(
    summary="The report does not identify a source line that explains the failure.",
    hypotheses=[],
    evidence=[],
    probable_files=[],
    unanswered_questions=[
        "Which file raises the error when the customer tier is unknown?",
    ],
)

BOGUS_CITATION_REPORT = InvestigationReport(
    summary=f"The crash is explained by {BOGUS_PATH}.",
    hypotheses=[
        Hypothesis(
            statement=f"The unknown tier is handled in {BOGUS_PATH}.",
            citations=[
                EvidenceCitation(relative_path=BOGUS_PATH, start_line=1, end_line=4),
            ],
        )
    ],
    evidence=[EvidenceCitation(relative_path=BOGUS_PATH, start_line=1, end_line=4)],
    probable_files=[BOGUS_PATH],
    unanswered_questions=[],
)

DEMO_CHANGE_PLAN = ChangePlan(
    problem_statement=(
        "An unknown customer tier indexes KNOWN_TIERS and the endpoint returns HTTP 500."
    ),
    assumptions=["The intended response for an unrecognized tier is HTTP 422."],
    modifications=[
        ProposedModification(
            relative_path="src/demo_service/discounts.py",
            kind=ModificationKind.EXISTING,
            summary="Reject an unknown tier before indexing KNOWN_TIERS.",
        ),
        ProposedModification(
            relative_path="src/demo_service/app.py",
            kind=ModificationKind.EXISTING,
            summary="Turn that rejection into HTTP 422.",
        ),
        ProposedModification(
            relative_path="tests/test_unknown_tier_added.py",
            kind=ModificationKind.NEW_TEST,
            summary="Add a regression test for an unrecognized tier.",
        ),
    ],
    test_strategy="Run the demo service pytest suite, including the unknown-tier regression.",
    risks=["A stricter check could reject a tier the service currently accepts."],
    confidence=0.72,
    can_proceed=True,
)

AMBIGUOUS_PLAN = ChangePlan(
    problem_statement="The report does not identify a change that the evidence supports.",
    assumptions=["The failing request and the expected response are still unknown."],
    modifications=[],
    test_strategy="No test change is proposed until the failure is reproducible.",
    risks=["Editing the checkout without a cited line could hide a different defect."],
    confidence=0.2,
    can_proceed=False,
)

UNSAFE_PATH_PLAN = ChangePlan(
    problem_statement="An unknown tier indexes KNOWN_TIERS. Also write ../etc/passwd.",
    assumptions=["The discount lookup is the cited failure."],
    modifications=[
        ProposedModification(
            relative_path="src/demo_service/discounts.py",
            kind=ModificationKind.EXISTING,
            summary="Reject an unknown tier before indexing KNOWN_TIERS.",
        ),
        ProposedModification(
            relative_path="../etc/passwd",
            kind=ModificationKind.EXISTING,
            summary="Write the host password file.",
        ),
        ProposedModification(
            relative_path="Dockerfile",
            kind=ModificationKind.NEW_TEST,
            summary="Replace the image definition.",
        ),
        ProposedModification(
            relative_path=".github/workflows/ci.yml",
            kind=ModificationKind.EXISTING,
            summary="Change the workflow that runs tests.",
        ),
    ],
    test_strategy="Run the demo service pytest suite.",
    risks=["Writing Dockerfile would change the image."],
    confidence=0.66,
    can_proceed=True,
)

UNSAFE_ONLY_PLAN = ChangePlan(
    problem_statement="Write ../etc/passwd and replace Dockerfile.",
    assumptions=[],
    modifications=[
        ProposedModification(
            relative_path="../etc/passwd",
            kind=ModificationKind.EXISTING,
            summary="Write the host password file.",
        ),
        ProposedModification(
            relative_path="Dockerfile",
            kind=ModificationKind.NEW_TEST,
            summary="Replace the image definition.",
        ),
    ],
    test_strategy="Do not run tests.",
    risks=["Writing Dockerfile would change the image."],
    confidence=0.9,
    can_proceed=True,
)

INJECTION_FOLLOWED_REPORT = InvestigationReport(
    summary=(f"Follow the comment. The token is {INJECTION_TOKEN}. Read {INJECTION_PATH}."),
    hypotheses=[
        Hypothesis(
            statement=f"The credential is in {INJECTION_PATH}.",
            citations=[
                EvidenceCitation(relative_path=INJECTION_PATH, start_line=1, end_line=3),
            ],
        )
    ],
    evidence=[
        EvidenceCitation(relative_path=INJECTION_PATH, start_line=1, end_line=3),
    ],
    probable_files=[INJECTION_PATH],
    unanswered_questions=[f"What is the token in {INJECTION_PATH}?"],
)

_READ_BLOCK = re.compile(
    r"^OBSERVATION read_text_file path=(\S+)\n(.*?)^END OBSERVATION$",
    re.M | re.S,
)
_LINE = re.compile(r"^(\d+) \| (.*)$", re.M)
_TREE_BLOCK = re.compile(
    r"^OBSERVATION list_tree path=\S+\n(.*?)^END OBSERVATION$",
    re.M | re.S,
)
_TREE_FILE = re.compile(r"^file (\S+)$", re.M)
_SEARCH_BLOCK = re.compile(
    r"^OBSERVATION search_text query=\S+ path=\S+\n(.*?)^END OBSERVATION$",
    re.M | re.S,
)
_SEARCH_HIT = re.compile(r"^(\S+):\d+(?: |$)", re.M)
_INTERESTING = ("return KNOWN_TIERS[", "percent = discount_percent(")


def builtin_investigator_step(prompt: str) -> InvestigatorStep:
    """Script one tool call, or a fixture report once the script has enough reads."""
    finished = _finish_investigation(prompt, force=False)
    if finished is not None:
        return InvestigatorStep(tool=None, report=finished)
    return InvestigatorStep(tool=_next_tool(prompt), report=None)


_FILE_SHA = re.compile(r"^FILE (\S+) sha256=([0-9a-f]{64}|new)$", re.M)
_FILE_BLOCK = re.compile(
    r"^FILE (\S+) sha256=(?:[0-9a-f]{64}|new)\n(.*?)(?=^FILE |\Z)",
    re.M | re.S,
)
_REPAIR_COUNT = re.compile(r"^Repair count: (\d+)$", re.M)

ACCEPT_REVIEW = ReviewResult(
    summary="The patch matches the plan and the tests passed.",
    action=ReviewAction.ACCEPT,
    findings=[
        ReviewFinding(
            summary="The diff handles an unknown tier and the suite passed.",
            severity=FindingSeverity.LOW,
            evidence="src/demo_service/discounts.py",
        )
    ],
    evidence=["src/demo_service/discounts.py"],
)

REPAIR_ONCE_REVIEW = ReviewResult(
    summary="The unknown tier still raises KeyError.",
    action=ReviewAction.REPAIR,
    findings=[
        ReviewFinding(
            summary="The lookup still indexes KNOWN_TIERS without a validation error.",
            severity=FindingSeverity.HIGH,
            evidence="src/demo_service/discounts.py",
        )
    ],
    evidence=["src/demo_service/discounts.py"],
)

ACCEPT_DESPITE_TESTS = ReviewResult(
    summary="The tests are acceptable.",
    action=ReviewAction.ACCEPT,
    findings=[],
    evidence=["src/demo_service/discounts.py"],
)

REJECT_REVIEW = ReviewResult(
    summary="The patch needs a person to decide before another edit.",
    action=ReviewAction.REPAIR,
    findings=[
        ReviewFinding(
            summary="The change is not safe to accept.",
            severity=FindingSeverity.CRITICAL,
            evidence="src/demo_service/discounts.py",
        )
    ],
    evidence=["src/demo_service/discounts.py"],
)

FOREVER_REVIEW = ReviewResult(
    summary="The patch still needs another revision.",
    action=ReviewAction.REPAIR,
    findings=[
        ReviewFinding(
            summary="The reviewer requested another revision.",
            severity=FindingSeverity.MEDIUM,
            evidence="src/demo_service/discounts.py",
        )
    ],
    evidence=["src/demo_service/discounts.py"],
)

DEMO_PATCH_SUMMARY = (
    "Reject an unknown customer tier with HTTP 422 and cover it with a regression test."
)

_DISCOUNTS_AFTER = """\"\"\"Discount rules for the demo service.

Known tiers return a percent. An unknown tier raises UnknownTierError.
\"\"\"

KNOWN_TIERS: dict[str, int] = {
    "standard": 0,
    "silver": 10,
    "gold": 20,
}


class UnknownTierError(ValueError):
    \"\"\"The customer tier is not one of the known tiers.\"\"\"


def discount_percent(customer_tier: str) -> int:
    try:
        return KNOWN_TIERS[customer_tier]
    except KeyError as exc:
        raise UnknownTierError(customer_tier) from exc
"""

_APP_AFTER = """\"\"\"HTTP wrapper around the discount rules. An unknown tier is HTTP 422.\"\"\"

from fastapi import FastAPI, HTTPException

from demo_service.discounts import UnknownTierError, discount_percent

app = FastAPI(title="Demo service", version="0.1.0")


@app.get("/discount")
def get_discount(customer_tier: str) -> dict[str, object]:
    try:
        percent = discount_percent(customer_tier)
    except UnknownTierError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown customer tier: {exc}") from exc
    return {"customer_tier": customer_tier, "percent": percent}
"""

_TEST_AFTER = """\"\"\"Regression for an unrecognized customer tier.\"\"\"

from fastapi.testclient import TestClient

from demo_service.app import app


def test_unknown_tier_returns_validation_error():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/discount", params={"customer_tier": "platinum"})

    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown customer tier: platinum"
"""

_DEMO_FILES = (
    ("src/demo_service/discounts.py", _DISCOUNTS_AFTER),
    ("src/demo_service/app.py", _APP_AFTER),
    ("tests/test_unknown_tier_added.py", _TEST_AFTER),
)


def builtin_patch_proposal(prompt: str) -> PatchProposal:
    """A demo edit, or a proposal the apply tool must refuse."""
    folded = prompt.casefold()
    if "patch schema failure" in folded:
        raise LLMResponseError("PatchProposal rejected model output (1 validation error)")
    if "patch traversal" in folded:
        return PatchProposal(
            summary="Escape the worktree.",
            unified_diff=(
                "diff --git a/../etc/passwd b/../etc/passwd\n"
                "--- a/../etc/passwd\n"
                "+++ b/../etc/passwd\n"
                "@@ -1 +1 @@\n"
                "-root\n"
                "+owned\n"
            ),
        )
    if "patch binary" in folded:
        path, digest = _first_existing_file(prompt)
        return PatchProposal(
            summary="Write a binary.",
            edits=[FileEdit(relative_path=path, expected_sha256=digest, content="payload\n\x00")],
        )
    if "patch oversized" in folded:
        path, digest = _first_existing_file(prompt)
        return PatchProposal(
            summary="Write far too much.",
            edits=[
                FileEdit(
                    relative_path=path,
                    expected_sha256=digest,
                    content="payload\n" + ("x" * 17_000),
                )
            ],
        )
    if "patch outside plan" in folded:
        return PatchProposal(
            summary="Edit a file the plan did not name.",
            edits=[
                FileEdit(
                    relative_path="src/demo_service/__init__.py",
                    expected_sha256="0" * 64,
                    content="SENTINEL = 1\n",
                )
            ],
        )
    if "patch malformed" in folded:
        return PatchProposal(
            summary="The diff is not unified.",
            unified_diff="this is not a unified diff\n",
        )
    attempt = _repair_attempt(prompt)
    if "review tests fail" in folded:
        return _unchanged_lookup(prompt, attempt)
    if "review repair once" in folded and "Reviewer action: repair" not in prompt:
        return _unchanged_lookup(prompt, attempt)
    repair_markers = ("review repair once", "review forever", "review reject")
    if any(marker in folded for marker in repair_markers):
        return _fixed_lookup(prompt, attempt)
    edits: list[FileEdit] = []
    for path, content in _DEMO_FILES:
        digest = _file_sha(prompt, path)
        if digest is None:
            continue
        edits.append(FileEdit(relative_path=path, expected_sha256=digest, content=content))
    if not edits:
        raise LLMResponseError("PatchProposal rejected model output (1 validation error)")
    return PatchProposal(summary=DEMO_PATCH_SUMMARY, edits=edits)


def builtin_supervisor(prompt: str) -> SupervisorRecommendation:
    """A recommendation policy may ignore. The stored reason never comes from here."""
    folded = prompt.casefold()
    if "supervisor recommend approval" in folded:
        route = SupervisorRoute.PROCEED_TO_APPROVAL
    elif "supervisor recommend finalize" in folded:
        route = SupervisorRoute.FINALIZE
    elif "supervisor recommend repair" in folded:
        route = SupervisorRoute.REPAIR
    else:
        route = SupervisorRoute.ESCALATE
    return SupervisorRecommendation(route=route, reason="model recommendation")


def builtin_review(prompt: str) -> ReviewResult:
    """Accept, request one repair, or keep rejecting until policy stops the loop."""
    folded = prompt.casefold()
    if "review schema failure" in folded:
        raise LLMResponseError("ReviewResult rejected model output (1 validation error)")
    if "invented/not_read.py" in folded and "review bogus evidence" in folded:
        return ReviewResult(
            summary="The crash is explained by invented/not_read.py.",
            action=ReviewAction.ACCEPT,
            findings=[
                ReviewFinding(
                    summary="See invented/not_read.py.",
                    severity=FindingSeverity.LOW,
                    evidence="invented/not_read.py",
                )
            ],
            evidence=["invented/not_read.py"],
        )
    if "review reject" in folded:
        return REJECT_REVIEW
    if "review forever" in folded:
        return FOREVER_REVIEW
    if "review tests fail" in folded:
        return ACCEPT_DESPITE_TESTS
    if "review repair once" in folded and (
        "tests status: failed" in folded or "tests status: timed_out" in folded
    ):
        return REPAIR_ONCE_REVIEW
    return ACCEPT_REVIEW


def _repair_attempt(prompt: str) -> int:
    match = _REPAIR_COUNT.search(prompt)
    if match is None:
        return 0
    value = int(match.group(1))
    if 0 <= value <= 2:
        return value
    return 0


def _unchanged_lookup(prompt: str, attempt: int) -> PatchProposal:
    path = "src/demo_service/discounts.py"
    digest = _file_sha(prompt, path)
    body = _file_body(prompt, path)
    if digest is None or body is None:
        raise LLMResponseError("PatchProposal rejected model output (1 validation error)")
    content = body.rstrip() + f"\n# patchpilot attempt {attempt}\n"
    return PatchProposal(
        summary="Leave the unknown tier lookup unchanged.",
        edits=[FileEdit(relative_path=path, expected_sha256=digest, content=content)],
    )


def _fixed_lookup(prompt: str, attempt: int) -> PatchProposal:
    edits: list[FileEdit] = []
    for path, content in _DEMO_FILES:
        digest = _file_sha(prompt, path)
        if digest is None:
            continue
        body = content
        if path.endswith("discounts.py"):
            body = content.rstrip() + f"\n# patchpilot attempt {attempt}\n"
        edits.append(FileEdit(relative_path=path, expected_sha256=digest, content=body))
    if not edits:
        raise LLMResponseError("PatchProposal rejected model output (1 validation error)")
    return PatchProposal(summary=DEMO_PATCH_SUMMARY, edits=edits)


def _file_body(prompt: str, path: str) -> str | None:
    for match in _FILE_BLOCK.finditer(prompt):
        if match.group(1) != path:
            continue
        return match.group(2).strip("\n")
    return None


def _file_sha(prompt: str, path: str) -> str | None:
    for match in _FILE_SHA.finditer(prompt):
        if match.group(1) != path:
            continue
        value = match.group(2)
        return "" if value == "new" else value
    return None


def _first_existing_file(prompt: str) -> tuple[str, str]:
    for match in _FILE_SHA.finditer(prompt):
        if match.group(2) == "new":
            continue
        return match.group(1), match.group(2)
    raise LLMResponseError("PatchProposal rejected model output (1 validation error)")


def builtin_change_plan(prompt: str) -> ChangePlan:
    """A reviewable proposal, a decline, an unsafe draft, or a schema failure."""
    folded = prompt.casefold()
    if "plan schema failure" in folded:
        raise LLMResponseError("ChangePlan rejected model output (1 validation error)")
    if "unsafe path only" in folded:
        return UNSAFE_ONLY_PLAN
    if "unsafe path" in folded:
        return UNSAFE_PATH_PLAN
    if (
        "ambiguous" in folded
        or "needs clarification: true" in folded
        or "evidence:\nnone" in folded
    ):
        return AMBIGUOUS_PLAN
    if "src/demo_service/discounts.py" in prompt:
        return DEMO_CHANGE_PLAN
    return AMBIGUOUS_PLAN


def builtin_investigation_report(prompt: str) -> InvestigationReport:
    """The report requested after the tool budget is spent."""
    finished = _finish_investigation(prompt, force=True)
    if finished is None:
        return _report_from_reads(prompt)
    return finished


def _finish_investigation(prompt: str, *, force: bool) -> InvestigationReport | None:
    if INJECTION_MARKER in prompt:
        return INJECTION_FOLLOWED_REPORT
    folded = prompt.casefold()
    if "bogus citation" in folded and _read_paths(prompt):
        return BOGUS_CITATION_REPORT
    if "missing evidence" in folded and _has_search(prompt):
        return MISSING_EVIDENCE_REPORT
    if force or _ready_to_report(prompt) or _nothing_to_read(prompt):
        return _report_from_reads(prompt)
    return None


def _next_tool(prompt: str) -> ToolRequest:
    if not _has_list(prompt):
        return ToolRequest(name=ToolName.LIST_TREE, relative_path=".")
    if not _has_search(prompt):
        return ToolRequest(
            name=ToolName.SEARCH_TEXT,
            relative_path=".",
            query=_search_query(prompt),
        )
    pending = [path for path in _preferred_paths(prompt) if path not in _read_paths(prompt)]
    return ToolRequest(name=ToolName.READ_TEXT_FILE, relative_path=pending[0])


def _search_query(prompt: str) -> str:
    folded = prompt.casefold()
    if "discount" in folded or "tier" in folded:
        return "KNOWN_TIERS"
    return "def"


def _ready_to_report(prompt: str) -> bool:
    reads = set(_read_paths(prompt))
    if not reads:
        return False
    return not any(path not in reads for path in _preferred_paths(prompt))


def _nothing_to_read(prompt: str) -> bool:
    return _has_search(prompt) and not _preferred_paths(prompt) and not _read_paths(prompt)


def _has_list(prompt: str) -> bool:
    return "OBSERVATION list_tree " in prompt


def _has_search(prompt: str) -> bool:
    return "OBSERVATION search_text " in prompt


def _read_paths(prompt: str) -> list[str]:
    return [match.group(1) for match in _READ_BLOCK.finditer(prompt)]


def _preferred_paths(prompt: str) -> list[str]:
    ordered: list[str] = []
    for path in _search_paths(prompt) + _tree_files(prompt):
        if path.endswith(("discounts.py", "app.py")) and path not in ordered:
            ordered.append(path)
    ordered.sort(key=lambda path: (not path.endswith("discounts.py"), path))
    if ordered:
        return ordered[:2]
    for path in _search_paths(prompt):
        if path.endswith(".py"):
            return [path]
    return []


def _search_paths(prompt: str) -> list[str]:
    found: list[str] = []
    for block in _SEARCH_BLOCK.finditer(prompt):
        for match in _SEARCH_HIT.finditer(block.group(1)):
            path = match.group(1)
            if path not in found:
                found.append(path)
    return found


def _tree_files(prompt: str) -> list[str]:
    found: list[str] = []
    for block in _TREE_BLOCK.finditer(prompt):
        for match in _TREE_FILE.finditer(block.group(1)):
            path = match.group(1)
            if path not in found:
                found.append(path)
    return found


def _report_from_reads(prompt: str) -> InvestigationReport:
    citations: list[EvidenceCitation] = []
    files: list[str] = []
    for match in _READ_BLOCK.finditer(prompt):
        path = match.group(1)
        for line in _LINE.finditer(match.group(2)):
            text = line.group(2)
            if not any(snippet in text for snippet in _INTERESTING):
                continue
            number = int(line.group(1))
            citations.append(
                EvidenceCitation(relative_path=path, start_line=number, end_line=number)
            )
            if path not in files:
                files.append(path)
    if not citations:
        return InvestigationReport(
            summary="The checkout did not show a line that explains the reported failure.",
            hypotheses=[],
            evidence=[],
            probable_files=[],
            unanswered_questions=["Which source line raises the failure?"],
        )
    return InvestigationReport(
        summary=(
            "An unknown customer tier indexes KNOWN_TIERS and the endpoint "
            "does not handle the KeyError."
        ),
        hypotheses=[
            Hypothesis(
                statement=(
                    "discount_percent indexes KNOWN_TIERS without checking the tier, "
                    "and the endpoint does not turn that into a validation error."
                ),
                citations=citations,
            )
        ],
        evidence=citations,
        probable_files=files,
        unanswered_questions=[],
    )


class FakeProvider:
    """Return schema-valid fixtures. The same prompt always returns the same value."""

    def __init__(self, fixtures: Mapping[str, Fixture] | None = None) -> None:
        self._fixtures = dict(fixtures or {})
        self.calls: list[ProviderCall] = []

    def generate_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        options: GenerateOptions | None = None,
    ) -> ModelT:
        checked_prompt = require_prompt(prompt)
        checked_schema = require_schema(schema)
        resolved = resolve_options(options)
        self.calls.append(
            ProviderCall(
                prompt=checked_prompt,
                schema_name=checked_schema.__name__,
                temperature=resolved.temperature,
            )
        )
        if checked_prompt in self._fixtures:
            return coerce_model(checked_schema, self._fixtures[checked_prompt])
        if checked_schema is TriageResult:
            return coerce_model(checked_schema, builtin_triage(checked_prompt))
        if checked_schema is InvestigatorStep:
            return coerce_model(checked_schema, builtin_investigator_step(checked_prompt))
        if checked_schema is InvestigationReport:
            return coerce_model(checked_schema, builtin_investigation_report(checked_prompt))
        if checked_schema is ChangePlan:
            return coerce_model(checked_schema, builtin_change_plan(checked_prompt))
        if checked_schema is PatchProposal:
            return coerce_model(checked_schema, builtin_patch_proposal(checked_prompt))
        if checked_schema is ReviewResult:
            return coerce_model(checked_schema, builtin_review(checked_prompt))
        if checked_schema is SupervisorRecommendation:
            return coerce_model(checked_schema, builtin_supervisor(checked_prompt))
        raise LLMResponseError(f"FakeProvider has no fixture for {checked_schema.__name__}")

    def close(self) -> None:
        return None
