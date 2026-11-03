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
from patchpilot.llm.provider import GenerateOptions, ModelT
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
        raise LLMResponseError(f"FakeProvider has no fixture for {checked_schema.__name__}")

    def close(self) -> None:
        return None
